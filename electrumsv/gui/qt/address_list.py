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

from functools import partial
import webbrowser

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QColor, QKeySequence
from PyQt5.QtWidgets import QTreeWidgetItem, QAbstractItemView, QMenu, QWidget
from bitcoinx import Address

from electrumsv.i18n import _
from electrumsv.app_state import app_state
from electrumsv.keystore import Hardware_KeyStore
from electrumsv.platform import platform
from electrumsv.util import profiler
from electrumsv.wallet import Multisig_Wallet, Abstract_Wallet
import electrumsv.web as web

from .util import MyTreeWidget, SortableTreeWidgetItem


class AddressList(MyTreeWidget):
    filter_columns = [0, 1, 2]  # Address, Label, Balance

    def __init__(self, parent: QWidget, initial_wallet: Abstract_Wallet) -> None:
        self.wallet = initial_wallet
        super().__init__(parent, self.create_menu, [], 2)
        self.monospace_font = QFont(platform.monospace_font)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.refresh_headers()

    def refresh_headers(self):
        headers = [ ('Address'), _('Index'),_('Label'), _('Balance'), _('Tx')]
        # Note this is dynamic with preferences changes
        if app_state.fx and app_state.fx.get_fiat_address_config():
            headers.insert(4, '{} {}'.format(app_state.fx.get_currency(), _(' Balance')))
        self.update_headers(headers)

    def on_update(self):
        self._on_update_address_list()

    @profiler
    def _on_update_address_list(self):
        def remember_expanded_items():
            # save the set of expanded items... so that address list updates don't
            # annoyingly collapse our tree list widget due to the update.
            expanded_item_names = set()
            for i in range(0, self.topLevelItemCount()):
                it = self.topLevelItem(i)
                if it and it.childCount():
                    if it.isExpanded():
                        expanded_item_names.add(it.text(0))
                    for j in range(0, it.childCount()):
                        it2 = it.child(j)
                        if it2 and it2.childCount() and it2.isExpanded():
                            expanded_item_names.add(it.text(0) + "/" + it2.text(0))
            return expanded_item_names

        def restore_expanded_items(seq_item, used_item, expanded_item_names):
            # restore expanded items.
            if (isinstance(seq_item, QTreeWidgetItem) and not seq_item.isExpanded() and
                   seq_item.text(0) in expanded_item_names):
                seq_item.setExpanded(True)
            used_item_name = (used_item.text(0) if not used_item.parent()
                              else used_item.parent().text(0) + "/" + used_item.text(0))
            if not used_item.isExpanded() and used_item_name in expanded_item_names:
                used_item.setExpanded(True)

        had_item_count = self.topLevelItemCount()
        item = self.currentItem()
        current_address = item.data(0, Qt.UserRole) if item else None
        expanded_item_names = remember_expanded_items()
        self.clear()
        receiving_addresses = self.wallet.get_receiving_addresses()[:]
        change_addresses = self.wallet.get_change_addresses()[:]

        account_item = self
        sequences = [0,1] if change_addresses else [0]
        if app_state.fx and app_state.fx.get_fiat_address_config():
            fx = app_state.fx
        else:
            fx = None
        for is_change in sequences:
            if len(sequences) > 1:
                name = _("Receiving") if not is_change else _("Change")
                seq_item = QTreeWidgetItem( [ name, '', '', '', '', ''] )
                account_item.addChild(seq_item)
                # first time we create this widget, auto-expand the default address list
                if not is_change and not had_item_count:
                    seq_item.setExpanded(True)
            else:
                seq_item = account_item
            used_item = QTreeWidgetItem( [ _("Used"), '', '', '', '', ''] )
            used_flag = False

            addr_list = change_addresses if is_change else receiving_addresses
            if self.wallet.is_deterministic():
                address_hashes = [ a.hash160() for a in addr_list ]
                gap_limit = (self.wallet.gap_limit_for_change if is_change
                    else self.wallet.gap_limit)

                limit_idx = None
                for i in range(len(addr_list)-1, -1, -1):
                    if self.wallet.get_address_history(addr_list[i]):
                        limit_idx = i + 1 + gap_limit
                        break

                def is_beyond_limit(address) -> bool:
                    idx = address_hashes.index(address.hash160())
                    ref_idx = idx - gap_limit
                    if ref_idx < 0 or limit_idx is None:
                        return False
                    return idx >= limit_idx
            else:
                def is_beyond_limit(address) -> bool:
                    return False

            for n, address in enumerate(addr_list):
                num = len(self.wallet.get_address_history(address))
                is_archived = self.wallet.is_archived_address(address)
                balance = sum(self.wallet.get_addr_balance(address))
                address_text = address.to_string()
                label = self.wallet.labels.get(address_text, '')
                balance_text = self.parent.format_amount(balance, whitespaces=True)
                columns = [address_text, str(n), label, balance_text, str(num)]
                if fx:
                    rate = fx.exchange_rate()
                    fiat_balance = fx.value_str(balance, rate)
                    columns.insert(4, fiat_balance)
                address_item = SortableTreeWidgetItem(columns)
                address_item.setTextAlignment(3, Qt.AlignRight)
                address_item.setFont(3, self.monospace_font)
                if fx:
                    address_item.setTextAlignment(4, Qt.AlignRight)
                    address_item.setFont(4, self.monospace_font)

                address_item.setFont(0, self.monospace_font)
                address_item.setData(0, Qt.UserRole, address)
                address_item.setData(0, Qt.UserRole+1, True) # label can be edited
                if self.wallet.is_frozen_address(address):
                    address_item.setBackground(0, QColor('lightblue'))
                if is_beyond_limit(address):
                    address_item.setBackground(0, QColor('red'))
                if is_archived:
                    if not used_flag:
                        seq_item.insertChild(0, used_item)
                        used_flag = True
                    used_item.addChild(address_item)
                else:
                    seq_item.addChild(address_item)
                if address == current_address:
                    self.setCurrentItem(address_item)
            restore_expanded_items(seq_item, used_item, expanded_item_names)

    def create_menu(self, position):
        is_multisig = isinstance(self.wallet, Multisig_Wallet)
        can_delete = self.wallet.can_delete_address()
        selected = self.selectedItems()
        multi_select = len(selected) > 1
        addrs = [item.data(0, Qt.UserRole) for item in selected]
        if not addrs:
            return
        addrs = [addr for addr in addrs if isinstance(addr, Address)]

        menu = QMenu()

        if not multi_select:
            item = self.itemAt(position)
            col = self.currentColumn()
            if not item:
                return
            if not addrs:
                item.setExpanded(not item.isExpanded())
                return
            addr = addrs[0]

            column_title = self.headerItem().text(col)
            if col == 0:
                copy_text = addr.to_string()
            else:
                copy_text = item.text(col).strip()
            menu.addAction(_("Copy {}").format(column_title),
                           lambda: self.parent.app.clipboard().setText(copy_text))
            menu.addAction(_('Details'), lambda: self.parent.show_address(self.wallet, addr))
            if col in self.editable_columns:
                menu.addAction(_("Edit {}").format(column_title), lambda: self.editItem(item, col))
            menu.addAction(_("Request payment"), lambda: self.parent.receive_at(addr))
            if self.wallet.can_export():
                menu.addAction(_("Private key"), lambda: self.parent.show_private_key(addr))
            if not is_multisig and not self.wallet.is_watching_only():
                menu.addAction(_("Sign/verify message"),
                               lambda: self.parent.sign_verify_message(self.wallet, addr))
                menu.addAction(_("Encrypt/decrypt message"),
                               lambda: self.encrypt_message(addr))
            if can_delete:
                menu.addAction(_("Remove from wallet"), lambda: self.parent.remove_address(addr))
            addr_URL = web.BE_URL(self.config, 'addr', addr)
            if addr_URL:
                menu.addAction(_("View on block explorer"), lambda: webbrowser.open(addr_URL))

            keystore = self.wallet.get_keystore()
            if self.wallet.wallet_type == 'standard' and isinstance(keystore, Hardware_KeyStore):
                def show_address():
                    self.parent.run_in_thread(keystore.plugin.show_address, self.wallet, addr)
                menu.addAction(_("Show on {}").format(keystore.plugin.device), show_address)

        freeze = self.parent.set_frozen_state
        if any(self.wallet.is_frozen_address(addr) for addr in addrs):
            menu.addAction(_("Unfreeze"), partial(freeze, self.wallet, addrs, False))
        if not all(self.wallet.is_frozen_address(addr) for addr in addrs):
            menu.addAction(_("Freeze"), partial(freeze, self.wallet, addrs, True))

        coins = self.wallet.get_spendable_coins(domain = addrs, config = self.config)
        if coins:
            menu.addAction(_("Spend from"),
                           partial(self.parent.spend_coins, coins))

        menu.exec_(self.viewport().mapToGlobal(position))

    def encrypt_message(self, addr):
        public_key_str = self.wallet.get_public_key(addr).to_hex() or ''
        self.parent.encrypt_message(self.wallet, public_key_str)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy) and self.currentColumn() == 0:
            addrs = [i.data(0, Qt.UserRole) for i in self.selectedItems()]
            if addrs and isinstance(addrs[0], Address):
                self.parent.app.clipboard().setText(addrs[0].to_string())
        else:
            super().keyPressEvent(event)
