#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2013 ecdsa@github
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

import math
import re

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QVBoxLayout, QGridLayout, QLabel, QCheckBox, QLineEdit, QWidget
)

from electrumsv.i18n import _
from electrumsv.wallet import ParentWallet

from .virtual_keyboard import VirtualKeyboard
from .util import (
    WindowModalDialog, OkButton, Buttons, CancelButton, icon_path, read_QIcon, ButtonsLineEdit,
)


def check_password_strength(password):

    '''
    Check the strength of the password entered by the user and return back the same
    :param password: password entered by user in New Password
    :return: password strength Weak or Medium or Strong
    '''

    password = password
    n = math.log(len(set(password)))
    num = re.search("[0-9]", password) is not None and re.match("^[0-9]*$", password) is None
    caps = password != password.upper() and password != password.lower()
    extra = re.match("^[a-zA-Z0-9]*$", password) is None
    score = len(password)*( n + caps + num + extra)/20
    password_strength = {0:"Weak",1:"Medium",2:"Strong",3:"Very Strong"}
    return password_strength[min(3, int(score))]


PW_NEW, PW_CHANGE, PW_PASSPHRASE = range(0, 3)


class PasswordLineEdit(QWidget):
    """
    Display a password QLineEdit with a button to open a virtual keyboard.
    """

    reveal_png = "icons8-eye-32.png"
    hide_png = "icons8-hide-32.png"

    def __init__(self, text=''):
        super().__init__()
        self.pw = ButtonsLineEdit(text)
        self.pw.setMinimumWidth(220)
        self.reveal_button = self.pw.addButton(self.reveal_png, self.toggle_visible,
                                               _("Toggle visibility"))
        self.reveal_button.setFocusPolicy(Qt.NoFocus)
        keyboard_button = self.pw.addButton("keyboard.png", self.toggle_keyboard,
            _("Virtual keyboard"))
        keyboard_button.setFocusPolicy(Qt.NoFocus)
        self.pw.setEchoMode(QLineEdit.Password)
        # self.pw.setMinimumWidth(200)
        self.keyboard = VirtualKeyboard(self.pw)
        self.keyboard.setVisible(False)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.setSizeConstraint(QVBoxLayout.SetFixedSize)
        layout.addWidget(self.pw)
        layout.addWidget(self.keyboard)
        self.setLayout(layout)

        # Pass-throughs
        self.returnPressed = self.pw.returnPressed
        self.setFocus = self.pw.setFocus
        self.setMaxLength = self.pw.setMaxLength
        self.setPlaceholderText = self.pw.setPlaceholderText
        self.setText = self.pw.setText
        self.setValidator = self.pw.setValidator
        self.text = self.pw.text
        self.textChanged = self.pw.textChanged
        self.editingFinished = self.pw.editingFinished
        self.textEdited = self.pw.textEdited

    def toggle_keyboard(self):
        self.keyboard.setVisible(not self.keyboard.isVisible())

    def toggle_visible(self):
        if self.pw.echoMode() == QLineEdit.Password:
            self.pw.setEchoMode(QLineEdit.Normal)
            self.reveal_button.setIcon(read_QIcon(self.hide_png))
        else:
            self.pw.setEchoMode(QLineEdit.Password)
            self.reveal_button.setIcon(read_QIcon(self.reveal_png))


class PasswordLayout(object):

    titles = [_("Enter Password"), _("Change Password"), _("Enter Passphrase")]

    def __init__(self, parent_wallet, msg, kind, OK_button) -> None:
        self.pw = PasswordLineEdit()
        self.new_pw = PasswordLineEdit()
        self.conf_pw = PasswordLineEdit()
        self.kind = kind
        self.OK_button = OK_button

        vbox = QVBoxLayout()
        label = QLabel(msg + "\n")
        label.setWordWrap(True)

        grid = QGridLayout()
        grid.setSpacing(8)
        grid.setColumnMinimumWidth(0, 150)
        grid.setColumnMinimumWidth(1, 100)
        grid.setColumnStretch(1,1)

        if kind == PW_PASSPHRASE:
            vbox.addWidget(label)
            msgs = [_('Passphrase:'), _('Confirm Passphrase:')]
        else:
            logo_grid = QGridLayout()
            logo_grid.setSpacing(8)
            logo_grid.setColumnMinimumWidth(0, 70)
            logo_grid.setColumnStretch(1,1)

            logo = QLabel()
            logo.setAlignment(Qt.AlignCenter)

            logo_grid.addWidget(logo,  0, 0)
            logo_grid.addWidget(label, 0, 1, 1, 2)
            vbox.addLayout(logo_grid)

            m1 = _('New Password:') if kind == PW_CHANGE else _('Password:')
            msgs = [m1, _('Confirm Password:')]
            if parent_wallet and parent_wallet.has_password():
                pwlabel = QLabel(_('Current Password:'))
                pwlabel.setAlignment(Qt.AlignTop)
                grid.addWidget(pwlabel, 0, 0)
                grid.addWidget(self.pw, 0, 1)
                lockfile = "lock.png"
            else:
                lockfile = "unlock.png"
            logo.setPixmap(QPixmap(icon_path(lockfile)).scaledToWidth(36))

        label0 = QLabel(msgs[0])
        label0.setAlignment(Qt.AlignTop)
        grid.addWidget(label0, 1, 0)
        grid.addWidget(self.new_pw, 1, 1)

        label1 = QLabel(msgs[1])
        label1.setAlignment(Qt.AlignTop)
        grid.addWidget(label1, 2, 0)
        grid.addWidget(self.conf_pw, 2, 1)
        vbox.addLayout(grid)

        # Password Strength Label
        if kind != PW_PASSPHRASE:
            self.pw_strength = QLabel()
            grid.addWidget(self.pw_strength, 3, 0, 1, 2)
            self.new_pw.textChanged.connect(self.pw_changed)

        self.encrypt_cb = QCheckBox(_('Encrypt wallet file'))
        self.encrypt_cb.setEnabled(False)
        grid.addWidget(self.encrypt_cb, 4, 0, 1, 2)
        self.encrypt_cb.setVisible(kind != PW_PASSPHRASE)

        def enable_OK():
            ok = self.new_pw.text() == self.conf_pw.text()
            OK_button.setEnabled(ok)
            self.encrypt_cb.setEnabled(ok and bool(self.new_pw.text()))
        self.new_pw.textChanged.connect(enable_OK)
        self.conf_pw.textChanged.connect(enable_OK)

        self.vbox = vbox

    def title(self):
        return self.titles[self.kind]

    def layout(self):
        return self.vbox

    def pw_changed(self):
        password = self.new_pw.text()
        if password:
            colors = {"Weak":"Red", "Medium":"Blue", "Strong":"Green",
                      "Very Strong":"Green"}
            strength = check_password_strength(password)
            label = (_("Password Strength") + ": " + "<font color="
                     + colors[strength] + ">" + strength + "</font>")
        else:
            label = ""
        self.pw_strength.setText(label)

    def old_password(self):
        if self.kind == PW_CHANGE:
            return self.pw.text() or None
        return None

    def new_password(self):
        pw = self.new_pw.text()
        # Empty passphrases are fine and returned empty.
        if pw == "" and self.kind != PW_PASSPHRASE:
            pw = None
        return pw


class ChangePasswordDialog(WindowModalDialog):
    def __init__(self, parent: QWidget, parent_wallet: ParentWallet) -> None:
        WindowModalDialog.__init__(self, parent)
        is_encrypted = parent_wallet.is_encrypted()
        if not parent_wallet.has_password():
            msg = _('Your wallet is not protected.')
            msg += ' ' + _('Use this dialog to add a password to your wallet.')
        else:
            if not is_encrypted:
                msg = _('Your bitcoins are password protected. However, your wallet file '
                        'is not encrypted.')
            else:
                msg = _('Your wallet is password protected and encrypted.')
            msg += ' ' + _('Use this dialog to change your password.')
        OK_button = OkButton(self)
        self.playout = PasswordLayout(parent_wallet, msg, PW_CHANGE, OK_button)
        self.setWindowTitle(self.playout.title())
        vbox = QVBoxLayout(self)
        vbox.setSizeConstraint(QVBoxLayout.SetFixedSize)
        vbox.addLayout(self.playout.layout())
        vbox.addStretch(1)
        vbox.addLayout(Buttons(CancelButton(self), OK_button))
        self.playout.encrypt_cb.setChecked(is_encrypted or not parent_wallet.has_password())

    def run(self):
        try:
            if not self.exec_():
                return False, None, None, None
            return (True, self.playout.old_password(), self.playout.new_password(),
                    self.playout.encrypt_cb.isChecked())
        finally:
            self.playout.pw.setText('')
            self.playout.conf_pw.setText('')
            self.playout.new_pw.setText('')


class PasswordDialog(WindowModalDialog):
    def __init__(self, parent=None, msg=None, force_keyboard=False):
        super().__init__(parent, _("Enter Password"))

        self.pw = pw = PasswordLineEdit()

        about_label = QLabel(msg or _('Enter your password:'))

        vbox = QVBoxLayout()
        vbox.addWidget(about_label)
        vbox.addWidget(pw)
        vbox.addStretch(1)
        vbox.addLayout(Buttons(CancelButton(self), OkButton(self)), Qt.AlignBottom)
        vbox.setSizeConstraint(QVBoxLayout.SetFixedSize)
        self.setLayout(vbox)

    def run(self):
        try:
            if not self.exec_():
                return None
            return self.pw.text()
        finally:
            self.pw.setText("")
