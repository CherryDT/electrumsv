from bitcoinx import Address

from electrumsv.wallet import Abstract_Wallet

from electrumsv.gui.qt.main_window import ElectrumWindow

from ..hw_wallet.qt import QtHandlerBase, QtPluginBase, HandlerWindow
from .digitalbitbox import DigitalBitboxPlugin


class Plugin(DigitalBitboxPlugin, QtPluginBase):
    icon_paired = "icons8-usb-connected-80.png"
    icon_unpaired = "icons8-usb-disconnected-80.png"

    def create_handler(self, window: HandlerWindow) -> QtHandlerBase:
        return DigitalBitbox_Handler(window)

    def show_address(self, wallet: Abstract_Wallet, address: Address) -> None:
        if not self.is_mobile_paired():
            return

        keystore = wallet.get_keystore()
        change, index = wallet.get_address_index(address)
        keypath = '%s/%d/%d' % (keystore.derivation, change, index)
        xpub = self.get_client(keystore)._get_xpub(keypath)
        verify_request_payload = {
            "type": 'p2pkh',
            "echo": xpub['echo'],
        }
        self.comserver_post_notification(verify_request_payload)


class DigitalBitbox_Handler(QtHandlerBase):

    def __init__(self, win):
        super(DigitalBitbox_Handler, self).__init__(win, 'Digital Bitbox')
