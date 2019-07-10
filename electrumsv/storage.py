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

import ast
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import zlib

from bitcoinx import PrivateKey, PublicKey

from .bitcoin import is_address_valid
from .keystore import bip44_derivation
from .logs import logs
from .networks import Net
from .util import profiler
from .wallet_database import DBTxInput, DBTxOutput, TxData, TxFlags, WalletData, MigrationContext


logger = logs.get_logger("storage")


# seed_version is now used for the version of the wallet file

OLD_SEED_VERSION = 4        # electrum versions < 2.0
NEW_SEED_VERSION = 11       # electrum versions >= 2.0
FINAL_SEED_VERSION = 19     # electrum >= 2.7 will set this to prevent
                            # old versions from overwriting new format


class IncompatibleWalletError(Exception):
    pass


def multisig_type(wallet_type):
    '''If wallet_type is mofn multi-sig, return [m, n],
    otherwise return None.'''
    if not wallet_type:
        return None
    match = re.match(r'(\d+)of(\d+)', wallet_type)
    if match:
        match = [int(x) for x in match.group(1, 2)]
    return match


class WalletStorage:
    def __init__(self, path, manual_upgrades=False):
        logger.debug("wallet path '%s'", path)
        dirname = os.path.dirname(path)
        if not os.path.exists(dirname):
            raise IOError(f'directory {dirname} does not exist')
        self.manual_upgrades = manual_upgrades
        self.lock = threading.RLock()
        self.data = {}
        self.path = path
        self._file_exists = self.path and os.path.exists(self.path) and os.path.isfile(self.path)
        self.modified = False
        self.pubkey = None
        if self.file_exists():
            try:
                with open(self.path, "r", encoding='utf-8') as f:
                    self.raw = f.read()
            except UnicodeDecodeError as e:
                raise IOError("Error reading file: "+ str(e))
            if not self.is_encrypted():
                self.load_data(self.raw)
        else:
            # Initialise anything that needs to be in the wallet storage and immediately persisted.
            # In the case of the aeskey, this is because the wallet saving is not guaranteed and
            # the writes to the database are not synchronised with it.
            tx_store_aeskey_hex = os.urandom(32).hex()
            self.put('tx_store_aeskey', tx_store_aeskey_hex)

            # avoid new wallets getting 'upgraded'
            self.put('wallet_author', 'ESV')
            self.put('seed_version', FINAL_SEED_VERSION)

    def load_data(self, s):
        try:
            self.data = json.loads(s)
        except:
            try:
                d = ast.literal_eval(s)
                labels = d.get('labels', {})
            except Exception as e:
                raise IOError("Cannot read wallet file '%s'" % self.path)
            self.data = {}
            for key, value in d.items():
                try:
                    json.dumps(key)
                    json.dumps(value)
                except:
                    logger.error('Failed to convert label to json format %s', key)
                    continue
                self.data[key] = value

        if not self.manual_upgrades:
            if self.requires_split():
                raise Exception("This wallet has multiple accounts and must be split")
            if self.requires_upgrade():
                self.upgrade()

    def is_encrypted(self):
        try:
            return base64.b64decode(self.raw)[0:4] == b'BIE1'
        except:
            return False

    def file_exists(self):
        """
        We preserve the existence of the file, in order to detect the case where the user selects
        a wallet, and then goes into the folder and deletes it unbeknownst to ElectrumSV.
        """
        return self._file_exists

    @staticmethod
    def get_eckey_from_password(password):
        secret = hashlib.pbkdf2_hmac('sha512', password.encode('utf-8'), b'', iterations=1024)
        return PrivateKey.from_arbitrary_bytes(secret)

    def decrypt(self, password):
        ec_key = self.get_eckey_from_password(password)
        if self.raw:
            s = zlib.decompress(ec_key.decrypt_message(self.raw))
        else:
            s = None
        self.pubkey = ec_key.public_key.to_hex()
        s = s.decode('utf8')
        self.load_data(s)

    def set_password(self, password, encrypt):
        self.put('use_encryption', bool(password))
        if encrypt and password:
            ec_key = self.get_eckey_from_password(password)
            self.pubkey = ec_key.public_key.to_hex()
        else:
            self.pubkey = None

    def get(self, key, default=None):
        with self.lock:
            v = self.data.get(key)
            if v is None:
                v = default
            else:
                v = copy.deepcopy(v)
        return v

    def put(self, key, value):
        try:
            json.dumps(key)
            json.dumps(value)
        except:
            logger.error("json error: cannot save %s", key)
            return
        with self.lock:
            if value is not None:
                if self.data.get(key) != value:
                    self.modified = True
                    self.data[key] = copy.deepcopy(value)
            elif key in self.data:
                self.modified = True
                self.data.pop(key)

    @profiler
    def write(self):
        with self.lock:
            self._write()

    def _write(self):
        if threading.currentThread().isDaemon():
            logger.error('daemon thread cannot write wallet')
            return
        if not self.modified:
            return
        s = json.dumps(self.data, indent=4, sort_keys=True)
        if self.pubkey:
            c = zlib.compress(s.encode())
            s = PublicKey.from_hex(self.pubkey).encrypt_message_to_base64(c)

        temp_path = "%s.tmp.%s" % (self.path, os.getpid())
        with open(temp_path, "w", encoding='utf-8') as f:
            f.write(s)
            f.flush()
            os.fsync(f.fileno())

        mode = os.stat(self.path).st_mode if self.file_exists() else stat.S_IREAD | stat.S_IWRITE
        # perform atomic write on POSIX systems
        try:
            os.rename(temp_path, self.path)
        except:
            os.remove(self.path)
            os.rename(temp_path, self.path)
        os.chmod(self.path, mode)
        self._file_exists = True
        self.raw = s
        logger.debug("saved '%s'", self.path)
        self.modified = False

    def requires_split(self):
        d = self.get('accounts', {})
        return len(d) > 1

    def split_accounts(self):
        result = []
        # backward compatibility with old wallets
        d = self.get('accounts', {})
        if len(d) < 2:
            return
        wallet_type = self.get('wallet_type')
        if wallet_type == 'old':
            assert len(d) == 2
            storage1 = WalletStorage(self.path + '.deterministic')
            storage1.data = copy.deepcopy(self.data)
            storage1.put('accounts', {'0': d['0']})
            storage1.upgrade()
            storage1.write()
            storage2 = WalletStorage(self.path + '.imported')
            storage2.data = copy.deepcopy(self.data)
            storage2.put('accounts', {'/x': d['/x']})
            storage2.put('seed', None)
            storage2.put('seed_version', None)
            storage2.put('master_public_key', None)
            storage2.put('wallet_type', 'imported')
            storage2.upgrade()
            storage2.write()
            result = [storage1.path, storage2.path]
        elif wallet_type in ['bip44', 'trezor', 'keepkey', 'ledger', 'btchip', 'digitalbitbox']:
            mpk = self.get('master_public_keys')
            for k in d.keys():
                i = int(k)
                x = d[k]
                if x.get("pending"):
                    continue
                xpub = mpk["x/%d'"%i]
                new_path = self.path + '.' + k
                storage2 = WalletStorage(new_path)
                storage2.data = copy.deepcopy(self.data)
                # save account, derivation and xpub at index 0
                storage2.put('accounts', {'0': x})
                storage2.put('master_public_keys', {"x/0'": xpub})
                storage2.put('derivation', bip44_derivation(k))
                storage2.upgrade()
                storage2.write()
                result.append(new_path)
        else:
            raise Exception("This wallet has multiple accounts and must be split")
        return result

    def requires_upgrade(self) -> None:
        if self.file_exists():
            seed_version = self.get_seed_version()
            # The version at which we should retain compatibility with Electrum and Electron Cash
            # if they upgrade their wallets using this versioning system correctly.
            if seed_version <= 17:
                return True
            # Versions above the compatible seed version, which may conflict with versions those
            # other wallets use.
            if seed_version < FINAL_SEED_VERSION:
                # We flag our upgraded wallets past seed version 17 with 'wallet_author' = 'ESV'.
                if self.get('wallet_author') == 'ESV':
                    return True
                raise IncompatibleWalletError
        return False

    def upgrade(self) -> None:
        logger.debug('upgrading wallet format')

        self._backup_wallet()
        self.convert_imported()
        self.convert_wallet_type()
        self.convert_account()
        self.convert_version_13_b()
        self.convert_version_14()
        self.convert_version_15()
        self.convert_version_16()
        self.convert_version_17()
        self.convert_version_18()
        self.convert_version_19()

        self.put('seed_version', FINAL_SEED_VERSION)  # just to be sure
        self.write()

    _wallet_backup_pattern = "%s.backup.%d"

    def _get_wallet_backup_path(self) -> str:
        attempt = 1
        while True:
            new_wallet_path = self._wallet_backup_pattern % (self.path, attempt)
            if not os.path.exists(new_wallet_path):
                return new_wallet_path
            attempt += 1

    def _backup_wallet(self) -> None:
        if not self.file_exists():
            return
        new_wallet_path = self._get_wallet_backup_path()
        shutil.copyfile(self.path, new_wallet_path)

    def convert_wallet_type(self) -> None:
        if not self._is_upgrade_method_needed(0, 13):
            return

        wallet_type = self.get('wallet_type')
        if wallet_type == 'btchip': wallet_type = 'ledger'
        if self.get('keystore') or self.get('x1/') or wallet_type=='imported':
            return False
        assert not self.requires_split()
        seed_version = self.get_seed_version()
        seed = self.get('seed')
        xpubs = self.get('master_public_keys')
        xprvs = self.get('master_private_keys', {})
        mpk = self.get('master_public_key')
        keypairs = self.get('keypairs')
        key_type = self.get('key_type')
        if seed_version == OLD_SEED_VERSION or wallet_type == 'old':
            d = {
                'type': 'old',
                'seed': seed,
                'mpk': mpk,
            }
            self.put('wallet_type', 'standard')
            self.put('keystore', d)

        elif key_type == 'imported':
            d = {
                'type': 'imported',
                'keypairs': keypairs,
            }
            self.put('wallet_type', 'standard')
            self.put('keystore', d)

        elif wallet_type in ['xpub', 'standard']:
            xpub = xpubs["x/"]
            xprv = xprvs.get("x/")
            d = {
                'type': 'bip32',
                'xpub': xpub,
                'xprv': xprv,
                'seed': seed,
            }
            self.put('wallet_type', 'standard')
            self.put('keystore', d)

        elif wallet_type in ['bip44']:
            xpub = xpubs["x/0'"]
            xprv = xprvs.get("x/0'")
            d = {
                'type': 'bip32',
                'xpub': xpub,
                'xprv': xprv,
            }
            self.put('wallet_type', 'standard')
            self.put('keystore', d)

        elif wallet_type in ['trezor', 'keepkey', 'ledger', 'digitalbitbox']:
            xpub = xpubs["x/0'"]
            derivation = self.get('derivation', bip44_derivation(0))
            d = {
                'type': 'hardware',
                'hw_type': wallet_type,
                'xpub': xpub,
                'derivation': derivation,
            }
            self.put('wallet_type', 'standard')
            self.put('keystore', d)

        elif multisig_type(wallet_type):
            for key in xpubs.keys():
                d = {
                    'type': 'bip32',
                    'xpub': xpubs[key],
                    'xprv': xprvs.get(key),
                }
                if key == 'x1/' and seed:
                    d['seed'] = seed
                self.put(key, d)
        else:
            raise Exception('Unable to tell wallet type. Is this even a wallet file?')
        # remove junk
        self.put('master_public_key', None)
        self.put('master_public_keys', None)
        self.put('master_private_keys', None)
        self.put('derivation', None)
        self.put('seed', None)
        self.put('keypairs', None)
        self.put('key_type', None)

    def convert_version_13_b(self):
        # version 13 is ambiguous, and has an earlier and a later structure
        if not self._is_upgrade_method_needed(0, 13):
            return

        if self.get('wallet_type') == 'standard':
            if self.get('keystore').get('type') == 'imported':
                pubkeys = self.get('keystore').get('keypairs').keys()
                d = {'change': []}
                receiving_addresses = []
                for pubkey in pubkeys:
                    addr = PublicKey.from_hex(pubkey).to_address(coin=Net.COIN).to_string()
                    receiving_addresses.append(addr)
                d['receiving'] = receiving_addresses
                self.put('addresses', d)
                self.put('pubkeys', None)

        self.put('seed_version', 13)

    def convert_version_14(self):
        # convert imported wallets for 3.0
        if not self._is_upgrade_method_needed(13, 13):
            return

        if self.get('wallet_type') =='imported':
            addresses = self.get('addresses')
            if type(addresses) is list:
                addresses = dict([(x, None) for x in addresses])
                self.put('addresses', addresses)
        elif self.get('wallet_type') == 'standard':
            if self.get('keystore').get('type')=='imported':
                addresses = set(self.get('addresses').get('receiving'))
                pubkeys = self.get('keystore').get('keypairs').keys()
                assert len(addresses) == len(pubkeys)
                d = {}
                for pubkey in pubkeys:
                    addr = PublicKey.from_hex(pubkey).to_address(coin=Net.COIN).to_string()
                    assert addr in addresses
                    d[addr] = {
                        'pubkey': pubkey,
                        'redeem_script': None,
                        'type': 'p2pkh'
                    }
                self.put('addresses', d)
                self.put('pubkeys', None)
                self.put('wallet_type', 'imported')
        self.put('seed_version', 14)

    def convert_version_15(self):
        if not self._is_upgrade_method_needed(14, 14):
            return
        self.put('seed_version', 15)

    def convert_version_16(self):
        # fixes issue #3193 for imported address wallets
        # also, previous versions allowed importing any garbage as an address
        #       which we now try to remove, see pr #3191
        if not self._is_upgrade_method_needed(15, 15):
            return

        def remove_address(addr):
            def remove_from_dict(dict_name):
                d = self.get(dict_name, None)
                if d is not None:
                    d.pop(addr, None)
                    self.put(dict_name, d)

            def remove_from_list(list_name):
                lst = self.get(list_name, None)
                if lst is not None:
                    s = set(lst)
                    s -= {addr}
                    self.put(list_name, list(s))

            # note: we don't remove 'addr' from self.get('addresses')
            remove_from_dict('addr_history')
            remove_from_dict('labels')
            remove_from_dict('payment_requests')
            remove_from_list('frozen_addresses')

        if self.get('wallet_type') == 'imported':
            addresses = self.get('addresses')
            assert isinstance(addresses, dict)
            addresses_new = dict()
            for address, details in addresses.items():
                if not is_address_valid(address):
                    remove_address(address)
                    continue
                if details is None:
                    addresses_new[address] = {}
                else:
                    addresses_new[address] = details
            self.put('addresses', addresses_new)

        self.put('seed_version', 16)

    def convert_version_17(self):
        if not self._is_upgrade_method_needed(16, 16):
            return
        if self.get('wallet_type') == 'imported':
            addrs = self.get('addresses')
            if all(v for v in addrs.values()):
                self.put('wallet_type', 'imported_privkey')
            else:
                self.put('wallet_type', 'imported_addr')

        self.put('seed_version', 17)

    def convert_version_18(self):
        if not self._is_upgrade_method_needed(17, 17):
            return

        # The scope of this change is to move the bulk of the data stored in the encrypted JSON
        # wallet file, into encrypted external storage.  At the time of the change, this
        # storage is based on an Sqlite database.

        wallet_type = self.get('wallet_type')

        tx_store_aeskey_hex = self.get('tx_store_aeskey')
        if tx_store_aeskey_hex is None:
            tx_store_aeskey_hex = os.urandom(32).hex()
            self.put('tx_store_aeskey', tx_store_aeskey_hex)
        tx_store_aeskey = bytes.fromhex(tx_store_aeskey_hex)

        db = WalletData(self.path, tx_store_aeskey, 0)

        # Transaction-related data.
        tx_map_in = self.get('transactions', {})
        tx_fees = self.get('fees', {})
        tx_verified = self.get('verified_tx3', {})

        _history = self.get('addr_history',{})
        hh_map = {tx_hash: tx_height
                  for addr_history in _history.values()
                  for tx_hash, tx_height in addr_history}

        to_add = []
        for tx_id, tx in tx_map_in.items():
            payload = bytes.fromhex(str(tx))
            fee = tx_fees.get(tx_id, None)
            if tx_id in tx_verified:
                flags = TxFlags.StateCleared
                height, timestamp, position = tx_verified[tx_id]
            else:
                flags = TxFlags.StateSettled
                timestamp = position = None
                height = hh_map.get(tx_id)
            tx_data = TxData(height=height, fee=fee, position=position, timestamp=timestamp)
            to_add.append((tx_id, tx_data, payload, flags))
        if len(to_add):
            db.tx_store.add_many(to_add)

        # Address/utxo related data.
        txi = self.get('txi', {})
        to_add = []
        for tx_hash, address_entry in txi.items():
            for address_string, output_values in address_entry.items():
                for prevout_key, amount in output_values:
                    prevout_tx_hash, prev_idx = prevout_key.split(":")
                    txin = DBTxInput(address_string, prevout_tx_hash, int(prev_idx), amount)
                    to_add.append((tx_hash, txin))
        if len(to_add):
            db.txin_store.add_entries(to_add)

        txo = self.get('txo', {})
        to_add = []
        for tx_hash, address_entry in txo.items():
            for address_string, input_values in address_entry.items():
                for txout_n, amount, is_coinbase in input_values:
                    txout = DBTxOutput(address_string, txout_n, amount, is_coinbase)
                    to_add.append((tx_hash, txout))
        if len(to_add):
            db.txout_store.add_entries(to_add)

        addresses = self.get('addresses')
        if addresses is not None:
            # Bug in the wallet storage upgrade tests, it turns this into a dict.
            if wallet_type == "imported_addr" and type(addresses) is dict:
                addresses = list(addresses.keys())
            db.misc_store.add('addresses', addresses)
        db.misc_store.add('addr_history', self.get('addr_history'))
        db.misc_store.add('frozen_addresses', self.get('frozen_addresses'))

        # Convert from "hash:n" to (hash, n).
        frozen_coins = self.get('frozen_coins', [])
        for i, s in enumerate(frozen_coins):
            hash, n = s.split(":")
            n = int(n)
            frozen_coins[i] = (hash, n)
        db.misc_store.add('frozen_coins', frozen_coins)

        pruned_txo = self.get('pruned_txo', {})
        new_pruned_txo = {}
        for k, v in pruned_txo.items():
            hash, n = k.split(":")
            n = int(n)
            new_pruned_txo[(hash, n)] = v
        db.misc_store.add('pruned_txo', new_pruned_txo)

        # One database connection is shared, so only one is closable.
        db.tx_store.close()

        self.put('addresses', None)
        self.put('addr_history', None)
        self.put('frozen_addresses', None)
        self.put('frozen_coins', None)
        self.put('pruned_txo', None)
        self.put('transactions', None)
        self.put('txi', None)
        self.put('txo', None)
        self.put('tx_fees', None)
        self.put('verified_tx3', None)

        self.put('wallet_author', 'ESV')
        self.put('seed_version', 18)

    def convert_version_19(self):
        if not self._is_upgrade_method_needed(18, 18):
            return

        # The scope of this upgrade is the move towards a wallet no longer being a keystore,
        # but being a container for one or more child wallets. The goal of this change was to
        # prepare for a move towards an account-oriented interface.

        # 1. Create the containment for the child wallets.
        subwallets = []

        wallet_type = self.get('wallet_type')
        assert wallet_type is not None, "Wallet has no type"

        # Some of these fields are specific to the wallet type, and others are common.
        possible_wallet_fields = [ "gap_limit", "invoices", "keystore", "labels",
            "multiple_change", "payment_requests", "stored_height", "use_change" ]

        # 2. Move the local contents of this wallet into the first subwallet.
        wallet_data = {}
        wallet_data['wallet_type'] = wallet_type
        for field_name in possible_wallet_fields:
            field_value = self.get(field_name)
            if field_value is not None:
                wallet_data[field_name] = field_value
                self.put(field_name, None)

        # Special case for the multiple keystores for the multisig wallets.
        multsig_mn = multisig_type(wallet_type)
        if multsig_mn is not None:
            m, n = multsig_mn
            for i in range(n):
                name = 'x%d/'%(i+1)
                wallet_data[name] = self.get(name)
                self.put(field_name, None)

        # Linked to the wallet database GroupId column.
        wallet_data["id"] = 0

        subwallets.append(wallet_data)
        self.put('subwallets', subwallets)

        # Convert the database to designate child wallets.
        tx_store_aeskey = bytes.fromhex(self.get('tx_store_aeskey'))
        db = WalletData(self.path, tx_store_aeskey, 0, MigrationContext(18, 19))

        self.put('seed_version', 19)

    def convert_imported(self):
        if not self._is_upgrade_method_needed(0, 13):
            return

        # '/x' is the internal ID for imported accounts
        d = self.get('accounts', {}).get('/x', {}).get('imported',{})
        if not d:
            return False
        addresses = []
        keypairs = {}
        for addr, v in d.items():
            pubkey, privkey = v
            if privkey:
                keypairs[pubkey] = privkey
            else:
                addresses.append(addr)
        if addresses and keypairs:
            raise Exception('mixed addresses and privkeys')
        elif addresses:
            self.put('addresses', addresses)
            self.put('accounts', None)
        elif keypairs:
            self.put('wallet_type', 'standard')
            self.put('key_type', 'imported')
            self.put('keypairs', keypairs)
            self.put('accounts', None)
        else:
            raise Exception('no addresses or privkeys')

    def convert_account(self):
        if not self._is_upgrade_method_needed(0, 13):
            return

        self.put('accounts', None)

    def _is_upgrade_method_needed(self, min_version, max_version):
        cur_version = self.get_seed_version()
        if cur_version > max_version:
            return False
        elif cur_version < min_version:
            raise Exception(
                ('storage upgrade: unexpected version %d (should be %d-%d)'
                 % (cur_version, min_version, max_version)))
        else:
            return True

    def get_action(self):
        if not self.file_exists():
            return 'new'

    def get_seed_version(self):
        seed_version = self.get('seed_version')
        if not seed_version:
            seed_version = (OLD_SEED_VERSION if len(self.get('master_public_key','')) == 128
                            else NEW_SEED_VERSION)
        if seed_version > FINAL_SEED_VERSION:
            raise RuntimeError('This version of ElectrumSV is too old to open this wallet')
        if seed_version >=12:
            return seed_version
        if seed_version not in [OLD_SEED_VERSION, NEW_SEED_VERSION]:
            self.raise_unsupported_version(seed_version)
        return seed_version

    def raise_unsupported_version(self, seed_version):
        msg = "Your wallet has an unsupported seed version."
        msg += '\n\nWallet file: %s' % os.path.abspath(self.path)
        if seed_version in [5, 7, 8, 9, 10, 14]:
            msg += "\n\nTo open this wallet, try 'git checkout seed_v%d'"%seed_version
        if seed_version == 6:
            # version 1.9.8 created v6 wallets when an incorrect seed
            # was entered in the restore dialog
            msg += '\n\nThis file was created because of a bug in version 1.9.8.'
            if (self.get('master_public_keys') is None and
                self.get('master_private_keys') is None and
                self.get('imported_keys') is None):
                # pbkdf2 (at that time an additional dependency) was not included
                # with the binaries, and wallet creation aborted.
                msg += "\nIt does not contain any keys, and can safely be removed."
            else:
                # creation was complete if electrum was run from source
                msg += ("\nPlease open this file with Electrum 1.9.8, and move "
                        "your coins to a new wallet.")
        raise Exception(msg)
