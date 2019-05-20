#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 Thomas Voegtlin
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

import struct

from bitcoinx import (
    PublicKey, PrivateKey, bip32_key_from_string, base58_encode_check,
    Ops, hash_to_hex_str, der_signature_to_compact, InvalidSignatureError,
    Script, push_int, push_item,
    Address, P2SH_Address, P2PK_Output, TxOutput, classify_output_script,
    pack_byte, unpack_le_uint16, read_list, double_sha256,
)

from .bitcoin import to_bytes, push_script, int_to_hex, var_int
from .crypto import sha256d, hash_160
from .networks import Net
from .logs import logs
from .util import profiler, bfh, bh2u


NO_SIGNATURE = 'ff'
dummy_public_key = PublicKey.from_bytes(bytes(range(3, 36)))

logger = logs.get_logger("transaction")


def classify_tx_output(tx_output: TxOutput):
    # This returns a P2PKH_Address, P2SH_Address, P2PK_Output, OP_RETURN_Output,
    # P2MultiSig_Output or Unknown_Output
    return classify_output_script(tx_output.script_pubkey)


def tx_output_to_display_text(tx_output: TxOutput):
    kind = classify_tx_output(tx_output)
    if isinstance(kind, Address):
        text = kind.to_string(coin=Net.COIN)
    elif isinstance(kind, P2PK_Output):
        text = kind.public_key.hex()
    else:
        text = tx_output.script_pubkey.to_asm()
    return text, kind


def _validate_outputs(outputs):
    assert all(isinstance(output, TxOutput) for output in outputs)
    assert all(isinstance(output.script_pubkey, Script) for output in outputs)
    assert all(isinstance(output.value, int) for output in outputs)


class XPublicKey:

    def __init__(self, raw):
        if not isinstance(raw, (bytes, str)):
            raise TypeError(f'raw {raw} must be bytes or a string')
        try:
            self.raw = raw if isinstance(raw, bytes) else bytes.fromhex(raw)
            self.to_public_key()
        except (ValueError, AssertionError):
            raise ValueError(f'invalid XPublicKey: {raw}')

    def __eq__(self, other):
        return isinstance(other, XPublicKey) and self.raw == other.raw

    def __hash__(self):
        return hash(self.raw) + 1

    def _bip32_public_key(self):
        extended_key, path = self.bip32_extended_key_and_path()
        result = bip32_key_from_string(extended_key)
        for n in path:
            result = result.child(n)
        return result

    def _old_keystore_public_key(self):
        mpk, path = self.old_keystore_mpk_and_path()
        mpk = PublicKey.from_bytes(pack_byte(4) + mpk)
        delta = double_sha256(f'{path[1]}:{path[0]}:'.encode() + self.raw[1:65])
        return mpk.add(delta)

    def to_bytes(self):
        return self.raw

    def to_hex(self):
        return self.raw.hex()

    def kind(self):
        return self.raw[0]

    def is_bip32_key(self):
        return self.kind() == 0xff

    def bip32_extended_key(self):
        assert len(self.raw) == 83    # 1 + 78 + 2 + 2
        assert self.is_bip32_key()
        return base58_encode_check(self.raw[1:79])

    def bip32_extended_key_and_path(self):
        extended_key = self.bip32_extended_key()
        return extended_key, [unpack_le_uint16(self.raw[n: n+2])[0] for n in (79, 81)]

    def old_keystore_mpk_and_path(self):
        assert len(self.raw) == 69
        assert self.kind() == 0xfe
        mpk = self.raw[1:65]  # The public key bytes without the 0x04 prefix
        return mpk, [unpack_le_uint16(self.raw[n: n+2])[0] for n in (65, 67)]

    def to_public_key(self):
        '''Returns a PublicKey instance or an Address instance.'''
        kind = self.kind()
        if kind in {0x02, 0x03, 0x04}:
            return PublicKey.from_bytes(self.raw)
        if kind == 0xff:
            return self._bip32_public_key()
        if kind == 0xfe:
            return self._old_keystore_public_key()
        assert kind == 0xfd
        result = classify_output_script(Script(self.raw[1:]))
        assert isinstance(result, Address)
        result = (result.__class__)(result.hash160(), coin=Net.COIN)
        return result

    def to_public_key_hex(self):
        # Only used for the pubkeys array
        public_key = self.to_public_key()
        if isinstance(public_key, Address):
            return public_key.to_script_bytes().hex()
        return public_key.to_hex()

    def to_address(self):
        result = self.to_public_key()
        if not isinstance(result, Address):
            result = result.to_address(coin=Net.COIN)
        return result

    def is_compressed(self):
        return self.kind() not in (0x04, 0xfe)

    def __repr__(self):
        return f"XPublicKey('{self.raw.hex()}')"


class UnknownAddress(object):

    def to_string(self):
        return '<UnknownAddress>'

    def __str__(self):
        return self.to_string()

    def __repr__(self):
        return '<UnknownAddress>'


class SerializationError(Exception):
    """ Thrown when there's a problem deserializing or serializing """

class InputValueMissing(Exception):
    """ thrown when the value of an input is needed but not present """

class _BCDataStream(object):
    def __init__(self):
        self.input = None
        self.read_cursor = 0

    def clear(self):
        self.input = None
        self.read_cursor = 0

    def write(self, _bytes):  # Initialize with string of _bytes
        if self.input is None:
            self.input = bytes(_bytes)
        else:
            self.input += bytes(_bytes)

    def read_string(self, encoding='ascii'):
        # Strings are encoded depending on length:
        # 0 to 252 :  1-byte-length followed by bytes (if any)
        # 253 to 65,535 : byte'253' 2-byte-length followed by bytes
        # 65,536 to 4,294,967,295 : byte '254' 4-byte-length followed by bytes
        # ... and the Bitcoin client is coded to understand:
        # greater than 4,294,967,295 : byte '255' 8-byte-length followed by bytes of string
        # ... but I don't think it actually handles any strings that big.
        if self.input is None:
            raise SerializationError("call write(bytes) before trying to deserialize")

        length = self.read_compact_size()

        return self.read_bytes(length).decode(encoding)

    def write_string(self, string, encoding='ascii'):
        string = to_bytes(string, encoding)
        # Length-encoded as with read-string
        self.write_compact_size(len(string))
        self.write(string)

    def read_bytes(self, length):
        try:
            result = self.input[self.read_cursor:self.read_cursor+length]
            self.read_cursor += length
            return result
        except IndexError:
            raise SerializationError("attempt to read past end of buffer")

        return ''

    def read_boolean(self): return self.read_bytes(1)[0] != chr(0)
    def read_int16(self): return self._read_num('<h')
    def read_uint16(self): return self._read_num('<H')
    def read_int32(self): return self._read_num('<i')
    def read_uint32(self): return self._read_num('<I')
    def read_int64(self): return self._read_num('<q')
    def read_uint64(self): return self._read_num('<Q')

    def write_boolean(self, val): return self.write(chr(1) if val else chr(0))
    def write_int16(self, val): return self._write_num('<h', val)
    def write_uint16(self, val): return self._write_num('<H', val)
    def write_int32(self, val): return self._write_num('<i', val)
    def write_uint32(self, val): return self._write_num('<I', val)
    def write_int64(self, val): return self._write_num('<q', val)
    def write_uint64(self, val): return self._write_num('<Q', val)

    def read_compact_size(self):
        try:
            size = self.input[self.read_cursor]
            self.read_cursor += 1
            if size == 253:
                size = self._read_num('<H')
            elif size == 254:
                size = self._read_num('<I')
            elif size == 255:
                size = self._read_num('<Q')
            return size
        except IndexError:
            raise SerializationError("attempt to read past end of buffer")

    def write_compact_size(self, size):
        if size < 0:
            raise SerializationError("attempt to write size < 0")
        elif size < 253:
            self.write(bytes([size]))
        elif size < 2**16:
            self.write(b'\xfd')
            self._write_num('<H', size)
        elif size < 2**32:
            self.write(b'\xfe')
            self._write_num('<I', size)
        elif size < 2**64:
            self.write(b'\xff')
            self._write_num('<Q', size)

    def _read_num(self, format):
        try:
            (i,) = struct.unpack_from(format, self.input, self.read_cursor)
            self.read_cursor += struct.calcsize(format)
        except Exception as e:
            raise SerializationError(e)
        return i

    def _write_num(self, format, num):
        s = struct.pack(format, num)
        self.write(s)


def _script_GetOp(_bytes):
    i = 0
    blen = len(_bytes)
    while i < blen:
        vch = None
        opcode = _bytes[i]
        i += 1

        if opcode <= Ops.OP_PUSHDATA4:
            nSize = opcode
            if opcode == Ops.OP_PUSHDATA1:
                nSize = _bytes[i] if i < blen else 0
                i += 1
            elif opcode == Ops.OP_PUSHDATA2:
                # tolerate truncated script
                (nSize,) = struct.unpack_from('<H', _bytes, i) if i+2 <= blen else (0,)
                i += 2
            elif opcode == Ops.OP_PUSHDATA4:
                (nSize,) = struct.unpack_from('<I', _bytes, i) if i+4 <= blen else (0,)
                i += 4
            # array slicing here never throws exception even if truncated script
            vch = _bytes[i:i + nSize]
            i += nSize

        yield opcode, vch, i


def _match_decoded(decoded, to_match):
    if len(decoded) != len(to_match):
        return False
    for i in range(len(decoded)):
        # Ops below OP_PUSHDATA4 all just push data
        if (to_match[i] == Ops.OP_PUSHDATA4 and
                decoded[i][0] <= Ops.OP_PUSHDATA4 and decoded[i][0] > 0):
            continue
        if to_match[i] != decoded[i][0]:
            return False
    return True


def _parse_sig(x_sig):
    return [None if x == NO_SIGNATURE else x for x in x_sig]


def _parse_scriptSig(d, _bytes):
    try:
        decoded = list(_script_GetOp(_bytes))
    except Exception:
        # coinbase transactions raise an exception
        logger.exception("cannot find address in input script %s", bh2u(_bytes))
        return

    match = [ Ops.OP_PUSHDATA4 ]
    if _match_decoded(decoded, match):
        item = decoded[0][1]
        # payto_pubkey
        d['type'] = 'p2pk'
        d['signatures'] = [bh2u(item)]
        d['num_sig'] = 1
        d['x_pubkeys'] = []
        return

    # non-generated TxIn transactions push a signature
    # (seventy-something bytes) and then their public key
    # (65 bytes) onto the stack:
    match = [ Ops.OP_PUSHDATA4, Ops.OP_PUSHDATA4 ]
    if _match_decoded(decoded, match):
        sig = bh2u(decoded[0][1])
        x_pubkey = XPublicKey(decoded[1][1])
        try:
            signatures = _parse_sig([sig])
            address = x_pubkey.to_address()
        except:
            logger.exception("cannot find address in input script %s", bh2u(_bytes))
            return
        d['type'] = 'p2pkh'
        d['signatures'] = signatures
        d['x_pubkeys'] = [x_pubkey]
        d['num_sig'] = 1
        d['address'] = address
        return

    # p2sh transaction, m of n
    match = [ Ops.OP_0 ] + [ Ops.OP_PUSHDATA4 ] * (len(decoded) - 1)
    if not _match_decoded(decoded, match):
        logger.error("cannot find address in input script %s", bh2u(_bytes))
        return
    x_sig = [bh2u(x[1]) for x in decoded[1:-1]]
    m, n, x_pubkeys, address = _parse_redeemScript(decoded[-1][1])
    # write result in d
    d['type'] = 'p2sh'
    d['num_sig'] = m
    d['signatures'] = _parse_sig(x_sig)
    d['x_pubkeys'] = x_pubkeys
    d['address'] = address


def _parse_redeemScript(s):
    dec2 = [ x for x in _script_GetOp(s) ]
    m = dec2[0][0] - Ops.OP_1 + 1
    n = dec2[-2][0] - Ops.OP_1 + 1
    op_m = Ops.OP_1 + m - 1
    op_n = Ops.OP_1 + n - 1
    match_multisig = [ op_m ] + [Ops.OP_PUSHDATA4]*n + [ op_n, Ops.OP_CHECKMULTISIG ]
    if not _match_decoded(dec2, match_multisig):
        logger.error("cannot find address in input script %s", bh2u(s))
        return
    x_pubkeys = [XPublicKey(x[1]) for x in dec2[1:-2]]
    redeemScript = multisig_script(x_pubkeys, m)
    address = P2SH_Address(hash_160(redeemScript))
    return m, n, x_pubkeys, address


def _parse_input(vds):
    d = {}
    prevout_hash = hash_to_hex_str(vds.read_bytes(32))
    prevout_n = vds.read_uint32()
    scriptSig = vds.read_bytes(vds.read_compact_size())
    sequence = vds.read_uint32()
    d['prevout_hash'] = prevout_hash
    d['prevout_n'] = prevout_n
    d['sequence'] = sequence
    d['address'] = UnknownAddress()
    if prevout_hash == '00'*32:
        d['type'] = 'coinbase'
        d['scriptSig'] = bh2u(scriptSig)
    else:
        d['x_pubkeys'] = []
        d['signatures'] = {}
        d['address'] = None
        d['type'] = 'unknown'
        d['num_sig'] = 0
        d['scriptSig'] = bh2u(scriptSig)
        _parse_scriptSig(d, scriptSig)

        if not Transaction.is_txin_complete(d):
            d['value'] = vds.read_uint64()
    return d


def deserialize(raw):
    vds = _BCDataStream()
    vds.write(bfh(raw))

    d = {}
    d['version'] = vds.read_int32()
    n_vin = vds.read_compact_size()
    assert n_vin != 0
    d['inputs'] = [_parse_input(vds) for i in range(n_vin)]
    d['outputs'] = read_list(vds.read_bytes, TxOutput.read)
    d['lockTime'] = vds.read_uint32()
    return d


# pay & redeem scripts

def multisig_script(x_pubkeys, threshold):
    '''Returns bytes.

    x_pubkeys is an array of XPulicKey objects or an array of PublicKey objects.
    '''
    assert 1 <= threshold <= len(x_pubkeys)
    parts = [push_int(threshold)]
    parts.extend(push_item(x_pubkey.to_bytes()) for x_pubkey in x_pubkeys)
    parts.append(push_int(len(x_pubkeys)))
    parts.append(pack_byte(Ops.OP_CHECKMULTISIG))
    return b''.join(parts)


def tx_from_str(txt):
    "json or raw hexadecimal"
    import json
    txt = txt.strip()
    if not txt:
        raise ValueError("empty string")
    try:
        bfh(txt)
        is_hex = True
    except:
        is_hex = False
    if is_hex:
        return txt
    tx_dict = json.loads(str(txt))
    assert "hex" in tx_dict.keys()
    return tx_dict["hex"]



class Transaction:

    SIGHASH_FORKID = 0x40

    def __str__(self):
        if self.raw is None:
            self.raw = self.serialize()
        return self.raw

    def __init__(self, raw):
        if raw is None:
            self.raw = None
        elif isinstance(raw, str):
            self.raw = raw.strip() if raw else None
        elif isinstance(raw, dict):
            self.raw = raw['hex']
        else:
            raise Exception("cannot initialize transaction", raw)
        self._inputs = None
        self._outputs = None
        self.locktime = 0
        self.version = 1

    def update(self, raw):
        self.raw = raw
        self._inputs = None
        self.deserialize()

    def inputs(self):
        if self._inputs is None:
            self.deserialize()
        return self._inputs

    def outputs(self):
        if self._outputs is None:
            self.deserialize()
        return self._outputs

    def update_signatures(self, signatures):
        """Add new signatures to a transaction

        `signatures` is expected to be a list of binary sigs with signatures[i]
        intended for self._inputs[i], without the SIGHASH appended.
        This is used by hardware device code.
        """
        if self.is_complete():
            return
        if len(self.inputs()) != len(signatures):
            raise RuntimeError('expected {} signatures; got {}'
                               .format(len(self.inputs()), len(signatures)))
        for i, txin in enumerate(self.inputs()):
            sig = bh2u(signatures[i] + bytes([self.nHashType()]))
            logger.warning(f'Signature {i}: {sig}')
            if sig in txin.get('signatures'):
                continue
            pubkeys = [x_pubkey.to_public_key() for x_pubkey in txin['x_pubkeys']]
            pre_hash = self.preimage_hash(i)
            rec_sig_base = der_signature_to_compact(signatures[i])
            for recid in range(4):
                rec_sig = rec_sig_base + bytes([recid])
                try:
                    public_key = PublicKey.from_recoverable_signature(rec_sig, pre_hash, None)
                except (InvalidSignatureError, ValueError):
                    # the point might not be on the curve for some recid values
                    continue
                if public_key in pubkeys:
                    try:
                        public_key.verify_recoverable_signature(rec_sig, pre_hash, None)
                    except Exception:
                        logger.exception('')
                        continue
                    j = pubkeys.index(public_key)
                    logger.debug(f'adding sig {i} {j} {public_key} {sig}')
                    self.add_signature_to_txin(txin, j, sig)
                    break
        # redo raw
        self.raw = self.serialize()

    def add_signature_to_txin(self, txin, signingPos, sig):
        assert isinstance(sig, str)
        txin['signatures'][signingPos] = sig
        txin['scriptSig'] = None  # force re-serialization
        self.raw = None

    def deserialize(self) -> dict:
        if self.raw is None:
            return
        if self._inputs is not None:
            return
        d = deserialize(self.raw)
        self._inputs = d['inputs']
        self._outputs = d['outputs']
        _validate_outputs(self._outputs)
        self.locktime = d['lockTime']
        self.version = d['version']
        return d

    @classmethod
    def from_io(cls, inputs, outputs, locktime=0):
        _validate_outputs(outputs)
        self = cls(None)
        self._inputs = inputs
        self._outputs = outputs.copy()
        self.locktime = locktime
        return self

    @classmethod
    def pay_script(self, output):
        if isinstance(output, PublicKey):
            return output.P2PK_script().to_hex()
        if isinstance(output, Address):
            return output.to_script_bytes().hex()
        return output.to_hex()

    @classmethod
    def get_siglist(self, txin, estimate_size=False):
        # if we have enough signatures, we use the actual pubkeys
        # otherwise, use extended pubkeys (with bip32 derivation)
        num_sig = txin.get('num_sig', 1)
        if estimate_size:
            x_pubkeys = txin['x_pubkeys']
            dummy = XPublicKey(dummy_public_key.to_bytes(compressed=x_pubkeys[0].is_compressed()))
            x_pubkeys = [dummy] * len(x_pubkeys)
            # we assume that signature will be 0x48 bytes long
            sig_list = [ "00" * 0x48 ] * num_sig
        else:
            x_pubkeys = txin['x_pubkeys']
            x_signatures = txin['signatures']
            signatures = [sig for sig in x_signatures if sig]
            is_complete = len(signatures) == num_sig
            if is_complete:
                # Realise the x_pubkeys
                x_pubkeys = [XPublicKey(x_pubkey.to_public_key().to_bytes())
                             for x_pubkey in x_pubkeys]
                sig_list = signatures
            else:
                sig_list = [sig if sig else NO_SIGNATURE for sig in x_signatures]
        return x_pubkeys, sig_list

    @classmethod
    def input_script(self, txin, estimate_size=False):
        _type = txin['type']
        if _type == 'coinbase':
            return txin['scriptSig']
        x_pubkeys, sig_list = self.get_siglist(txin, estimate_size)
        script = ''.join(push_script(x) for x in sig_list)
        if _type == 'p2pk':
            pass
        elif _type == 'p2sh':
            # put op_0 before script
            script = '00' + script
            redeem_script = multisig_script(x_pubkeys, txin['num_sig']).hex()
            script += push_script(redeem_script)
        elif _type == 'p2pkh':
            script += push_script(x_pubkeys[0].to_hex())
        elif _type == 'unknown':
            return txin['scriptSig']
        return script

    @classmethod
    def is_txin_complete(self, txin):
        num_sig = txin.get('num_sig', 1)
        x_signatures = txin['signatures']
        signatures = [sig for sig in x_signatures if sig]
        return len(signatures) == num_sig

    @classmethod
    def get_preimage_script(self, txin):
        _type = txin['type']
        if _type == 'p2pkh':
            return txin['address'].to_script_bytes().hex()
        elif _type == 'p2sh':
            pubkeys = [x_pubkey.to_public_key() for x_pubkey in txin['x_pubkeys']]
            return multisig_script(pubkeys, txin['num_sig']).hex()
        elif _type == 'p2pk':
            x_pubkey = txin['x_pubkeys'][0]
            output = P2PK_Output(x_pubkey.to_public_key())
            return output.to_script_bytes().hex()
        elif _type == 'unknown':
            # this approach enables most P2SH smart contracts
            # (but take care if using OP_CODESEPARATOR)
            return txin['scriptCode']
        else:
            raise RuntimeError('Unknown txin type', _type)

    @classmethod
    def serialize_outpoint(self, txin):
        return bh2u(bfh(txin['prevout_hash'])[::-1]) + int_to_hex(txin['prevout_n'], 4)

    @classmethod
    def serialize_input(self, txin, script, estimate_size=False):
        # Prev hash and index
        s = self.serialize_outpoint(txin)
        # Script length, script, sequence
        s += var_int(len(script)//2)
        s += script
        s += int_to_hex(txin.get('sequence', 0xffffffff - 1), 4)
        # offline signing needs to know the input value
        if ('value' in txin   # Legacy txs
            and not (estimate_size or self.is_txin_complete(txin))):
            s += int_to_hex(txin['value'], 8)
        return s

    def BIP_LI01_sort(self):
        # See https://github.com/kristovatlas/rfc/blob/master/bips/bip-li01.mediawiki
        self._inputs.sort(key = lambda i: (i['prevout_hash'], i['prevout_n']))
        self._outputs.sort(key = lambda output: (output.value, output.script_pubkey))

    @classmethod
    def nHashType(cls):
        '''Hash type in hex.'''
        return 0x01 | cls.SIGHASH_FORKID

    def preimage_hash(self, txin_index):
        return sha256d(bfh(self.serialize_preimage(txin_index)))

    def serialize_preimage(self, i):
        nVersion = int_to_hex(self.version, 4)
        nHashType = int_to_hex(self.nHashType(), 4)
        nLocktime = int_to_hex(self.locktime, 4)
        inputs = self.inputs()
        outputs = self.outputs()
        txin = inputs[i]

        hashPrevouts = bh2u(sha256d(bfh(''.join(self.serialize_outpoint(txin) for txin in inputs))))
        hashSequence = bh2u(sha256d(bfh(''.join(int_to_hex(txin.get('sequence', 0xffffffff - 1), 4)
                                             for txin in inputs))))
        hashOutputs = bh2u(sha256d(b''.join(output.to_bytes() for output in outputs)))
        outpoint = self.serialize_outpoint(txin)
        preimage_script = self.get_preimage_script(txin)
        scriptCode = var_int(len(preimage_script) // 2) + preimage_script
        try:
            amount = int_to_hex(txin['value'], 8)
        except KeyError:
            raise InputValueMissing
        nSequence = int_to_hex(txin.get('sequence', 0xffffffff - 1), 4)
        preimage = (nVersion + hashPrevouts + hashSequence + outpoint +
                    scriptCode + amount + nSequence + hashOutputs + nLocktime + nHashType)
        return preimage

    def serialize(self, estimate_size=False):
        nVersion = int_to_hex(self.version, 4)
        nLocktime = int_to_hex(self.locktime, 4)
        inputs = self.inputs()
        outputs = self.outputs()
        txins = var_int(len(inputs)) + ''.join(
            self.serialize_input(txin, self.input_script(txin, estimate_size), estimate_size)
            for txin in inputs
        )
        txouts = var_int(len(outputs)) + b''.join(output.to_bytes() for output in outputs).hex()
        return nVersion + txins + txouts + nLocktime

    def txid(self):
        if not self.is_complete():
            return None
        ser = self.serialize()
        return bh2u(sha256d(bfh(ser))[::-1])

    def add_inputs(self, inputs):
        self._inputs.extend(inputs)
        self.raw = None

    def add_outputs(self, outputs):
        _validate_outputs(outputs)
        self._outputs.extend(outputs)
        self.raw = None

    def input_value(self):
        return sum(x['value'] for x in self.inputs())

    def output_value(self):
        return sum(output.value for output in self.outputs())

    def get_fee(self):
        return self.input_value() - self.output_value()

    @profiler
    def estimated_size(self):
        '''Return an estimated tx size in bytes.'''
        return (len(self.serialize(True)) // 2 if not self.is_complete() or self.raw is None
                else len(self.raw) // 2)  # ASCII hex string

    @classmethod
    def estimated_input_size(self, txin):
        '''Return an estimated of serialized input size in bytes.'''
        script = self.input_script(txin, True)
        return len(self.serialize_input(txin, script, True)) // 2  # ASCII hex string

    def signature_count(self):
        r = 0
        s = 0
        for txin in self.inputs():
            if txin['type'] == 'coinbase':
                continue
            signatures = [sig for sig in txin.get('signatures', []) if sig]
            s += len(signatures)
            r += txin.get('num_sig',-1)
        return s, r

    def is_complete(self):
        s, r = self.signature_count()
        return r == s

    def sign(self, keypairs):
        assert all(isinstance(key, XPublicKey) for key in keypairs)
        for i, txin in enumerate(self.inputs()):
            num = txin['num_sig']
            x_pubkeys = txin['x_pubkeys']
            for j, x_pubkey in enumerate(x_pubkeys):
                signatures = [sig for sig in txin['signatures'] if sig]
                if len(signatures) == num:
                    # txin is complete
                    break
                if x_pubkey in keypairs.keys():
                    logger.debug("adding signature for %s", x_pubkey)
                    sec, compressed = keypairs.get(x_pubkey)
                    sig = self.sign_txin(i, sec)
                    txin['signatures'][j] = sig
                    if x_pubkey.kind() == 0xfd:
                        pubkey_bytes = PrivateKey(sec).public_key.to_bytes(compressed=compressed)
                        x_pubkeys[j] = XPublicKey(pubkey_bytes)
                    self._inputs[i] = txin
        logger.debug("is_complete %s", self.is_complete())
        self.raw = self.serialize()

    def sign_txin(self, txin_index, privkey_bytes):
        pre_hash = self.preimage_hash(txin_index)
        privkey = PrivateKey(privkey_bytes)
        sig = privkey.sign(pre_hash, None)
        sig = bh2u(sig) + int_to_hex(self.nHashType(), 1)
        return sig

    def is_final(self):
        return not any([x.get('sequence', 0xffffffff - 1) < 0xffffffff - 1
                        for x in self.inputs()])

    def as_dict(self):
        if self.raw is None:
            self.raw = self.serialize()
        self.deserialize()
        out = {
            'hex': self.raw,
            'complete': self.is_complete(),
            'final': self.is_final(),
        }
        return out
