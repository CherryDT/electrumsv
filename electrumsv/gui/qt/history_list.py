#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import enum
import time
from typing import Union
import webbrowser

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QBrush, QFont, QIcon, QColor
from PyQt5.QtWidgets import (QListWidget, QListWidgetItem, QMenu, QSplitter, QWidget)

from electrumsv.app_state import app_state
from electrumsv.bitcoin import COINBASE_MATURITY
from electrumsv.i18n import _
from electrumsv.platform import platform
from electrumsv.util import timestamp_to_datetime, profiler, format_time

from electrumsv.wallet import Abstract_Wallet, ParentWallet
import electrumsv.web as web

from .main_window import ElectrumWindow
from .util import MyTreeWidget, SortableTreeWidgetItem, read_QIcon, MessageBox


class TxStatus(enum.IntEnum):
    MISSING = 0
    UNCONFIRMED = 1
    UNVERIFIED = 2
    UNMATURED = 3
    FINAL = 4

TX_ICONS = [
    "icons8-question-mark-96.png",      # Missing.
    "icons8-checkmark-grey-52.png",     # Unconfirmed.
    "icons8-checkmark-grey-52.png",     # Unverified.
    "icons8-lock-96.png",               # Unmatured.
    "icons8-checkmark-green-52.png",    # Confirmed / verified.
]

TX_STATUS = {
    TxStatus.FINAL: _('Confirmed'),
    TxStatus.MISSING: _('Missing'),
    TxStatus.UNCONFIRMED: _('Unconfirmed'),
    TxStatus.UNMATURED: _('Unmatured'),
    TxStatus.UNVERIFIED: _('Unverified'),
}


class HistoryList(MyTreeWidget):
    filter_columns = [2, 3, 4]  # Date, Description, Amount

    def __init__(self, parent: QWidget, wallet: Abstract_Wallet) -> None:
        MyTreeWidget.__init__(self, parent, self.create_menu, [], 3)
        self.wallet = wallet

        self.refresh_headers()
        self.setColumnHidden(1, True)
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.AscendingOrder)

        self.monospace_font = QFont(platform.monospace_font)
        self.withdrawalBrush = QBrush(QColor("#BC1E1E"))
        self.invoiceIcon = read_QIcon("seal")

    def refresh_headers(self):
        headers = ['', '', _('Date'), _('Description') , _('Amount'), _('Balance')]
        fx = app_state.fx
        if fx and fx.show_history():
            headers.extend(['%s '%fx.ccy + _('Amount'), '%s '%fx.ccy + _('Balance')])
        self.update_headers(headers)

    def get_domain(self):
        '''Replaced in address_dialog.py'''
        return self.wallet.get_addresses()

    def on_update(self):
        self._on_update_history_list()

    @profiler
    def _on_update_history_list(self):
        h = self.wallet.get_history(self.get_domain())
        item = self.currentItem()
        current_tx = item.data(0, Qt.UserRole) if item else None
        self.clear()
        fx = app_state.fx
        if fx:
            fx.history_used_spot = False
        for h_item in h:
            tx_hash, height, conf, timestamp, value, balance = h_item
            status = get_tx_status(self.wallet, tx_hash, height, conf, timestamp)
            status_str = get_tx_desc(status, timestamp)
            has_invoice = self.wallet.invoices.paid.get(tx_hash)
            icon = get_tx_icon(status)
            v_str = self.parent.format_amount(value, True, whitespaces=True)
            balance_str = self.parent.format_amount(balance, whitespaces=True)
            label = self.wallet.get_label(tx_hash)
            entry = ['', tx_hash, status_str, label, v_str, balance_str]
            if fx and fx.show_history():
                date = timestamp_to_datetime(time.time() if conf <= 0 else timestamp)
                for amount in [value, balance]:
                    text = fx.historical_value_str(amount, date)
                    entry.append(text)

            item = SortableTreeWidgetItem(entry)
            item.setIcon(0, icon)
            item.setToolTip(0, get_tx_tooltip(status, conf))
            item.setData(0, SortableTreeWidgetItem.DataRole, (status, conf))
            if has_invoice:
                item.setIcon(3, self.invoiceIcon)
            for i in range(len(entry)):
                if i>3:
                    item.setTextAlignment(i, Qt.AlignRight)
                if i!=2:
                    item.setFont(i, self.monospace_font)
            if value and value < 0:
                item.setForeground(3, self.withdrawalBrush)
                item.setForeground(4, self.withdrawalBrush)
            item.setData(0, Qt.UserRole, tx_hash)
            self.insertTopLevelItem(0, item)
            if current_tx == tx_hash:
                self.setCurrentItem(item)

    def on_doubleclick(self, item, column):
        if self.permit_edit(item, column):
            super(HistoryList, self).on_doubleclick(item, column)
        else:
            tx_hash = item.data(0, Qt.UserRole)
            tx = self.wallet.get_transaction(tx_hash)
            if tx is not None:
                self.parent.show_transaction(tx)
            else:
                MessageBox.show_error(_("The full transaction is not yet present in your wallet."+
                    " Please try again when it has been obtained from the network."))

    def update_labels(self):
        root = self.invisibleRootItem()
        child_count = root.childCount()
        for i in range(child_count):
            item = root.child(i)
            txid = item.data(0, Qt.UserRole)
            label = self.wallet.get_label(txid)
            item.setText(3, label)

    def update_item(self, tx_hash, height, conf, timestamp):
        status = get_tx_status(self.wallet, tx_hash, height, conf, timestamp)
        icon = get_tx_icon(status)
        items = self.findItems(tx_hash, Qt.UserRole|Qt.MatchContains|Qt.MatchRecursive, column=1)
        if items:
            item = items[0]
            item.setIcon(0, icon)
            item.setData(0, SortableTreeWidgetItem.DataRole, (status, conf))
            item.setText(2, get_tx_desc(status, timestamp))
            item.setToolTip(0, get_tx_tooltip(status, conf))

    def create_menu(self, position):
        self.selectedIndexes()
        item = self.currentItem()
        if not item:
            return
        column = self.currentColumn()
        tx_hash = item.data(0, Qt.UserRole)
        if not tx_hash:
            return
        if column == 0:
            column_title = "ID"
            column_data = tx_hash
        else:
            column_title = self.headerItem().text(column)
            column_data = item.text(column).strip()

        tx_URL = web.BE_URL(self.config, 'tx', tx_hash)
        height, _conf, _timestamp = self.wallet.get_tx_height(tx_hash)
        tx = self.wallet.get_transaction(tx_hash)
        if not tx: return # this happens sometimes on wallet synch when first starting up.
        # is_relevant, is_mine, v, fee = self.wallet.get_wallet_delta(tx)
        is_unconfirmed = height <= 0
        pr_key = self.wallet.invoices.paid.get(tx_hash)

        menu = QMenu()

        menu.addAction(_("Copy {}").format(column_title),
                       lambda: self.parent.app.clipboard().setText(column_data))
        if column in self.editable_columns:
            # We grab a fresh reference to the current item, as it has been deleted in a
            # reported issue.
            menu.addAction(_("Edit {}").format(column_title),
                lambda: self.currentItem() and self.editItem(self.currentItem(), column))
        label = self.wallet.get_label(tx_hash) or None
        menu.addAction(_("Details"), lambda: self.parent.show_transaction(tx, label))
        if is_unconfirmed and tx:
            child_tx = self.wallet.cpfp(tx, 0)
            if child_tx:
                menu.addAction(_("Child pays for parent"),
                    lambda: self.parent.cpfp(self.wallet, tx, child_tx))
        if pr_key:
            menu.addAction(read_QIcon("seal"), _("View invoice"),
                           lambda: self.parent.show_invoice(pr_key))
        if tx_URL:
            menu.addAction(_("View on block explorer"), lambda: webbrowser.open(tx_URL))
        menu.exec_(self.viewport().mapToGlobal(position))


def get_tx_status(wallet: Abstract_Wallet, tx_hash: str, height: int, conf: int,
        timestamp: Union[bool, int]) -> TxStatus:
    tx = wallet.get_transaction(tx_hash)
    if not tx:
        return TxStatus.MISSING

    if tx.is_coinbase():
        if height + COINBASE_MATURITY > wallet.get_local_height():
            return TxStatus.UNMATURED
    elif conf == 0:
        if height > 0:
            return TxStatus.UNVERIFIED
        return TxStatus.UNCONFIRMED

    return TxStatus.FINAL

def get_tx_desc(status: TxStatus, timestamp: Union[bool, int]) -> str:
    if status in [ TxStatus.UNCONFIRMED, TxStatus.MISSING ]:
        return TX_STATUS[status]
    return format_time(timestamp, _("unknown")) if timestamp else _("unknown")

def get_tx_tooltip(status: TxStatus, conf: int) -> str:
    text = str(conf) + " confirmation" + ("s" if conf != 1 else "")
    if status == TxStatus.UNMATURED:
        text = text + "\n" + _("Unmatured")
    elif status in TX_STATUS:
        text = text + "\n"+ TX_STATUS[status]
    return text

def get_tx_icon(status: TxStatus) -> QIcon:
    return read_QIcon(TX_ICONS[status])


class HistoryView(QSplitter):
    def __init__(self, parent: ElectrumWindow, parent_wallet: ParentWallet) -> None:
        super().__init__(parent)

        self._main_window = parent
        self._parent_wallet = parent_wallet

        # Left-hand side a list of wallets.
        # Right-hand side a history view showing the current wallet.
        self._selection_list = QListWidget()
        self._history_list = HistoryList(parent, parent_wallet.get_default_wallet())

        self.addWidget(self._selection_list)
        self.addWidget(self._history_list)

        self.setStretchFactor(1, 2)

        self._update_wallet_list()

    def _update_wallet_list(self) -> None:
        for child_wallet in self._parent_wallet.get_child_wallets():
            item = QListWidgetItem()
            item.setText(child_wallet.display_name())
            self._selection_list.addItem(item)

    @property
    def searchable_list(self) -> HistoryList:
        return self._history_list

    def update_tx_list(self) -> None:
        self._history_list.update()

    def update_tx_headers(self) -> None:
        self._history_list.update_headers()

    def update_tx_labels(self) -> None:
        self._history_list.update_labels()

    def update_tx_item(self, tx_hash: str, height, conf, timestamp) -> None:
        self._history_list.update_item(tx_hash, height, conf, timestamp)

