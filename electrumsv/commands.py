#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
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

import argparse
import ast
from decimal import Decimal
from functools import wraps
import json
import sys

from .bitcoin import COIN
from .i18n import _
from .logs import logs
from .util import json_decode

logger = logs.get_logger("commands")

known_commands = {}


def satoshis(amount):
    # satoshi conversion must not be performed by the parser
    return int(COIN*Decimal(amount)) if amount not in ['!', None] else amount


class Command:
    def __init__(self, func, s):
        self.name = func.__name__
        self.requires_network = 'n' in s
        self.requires_wallet = 'w' in s
        self.requires_password = 'p' in s
        self.description = func.__doc__
        self.help = self.description.split('.')[0] if self.description else None
        varnames = func.__code__.co_varnames[1:func.__code__.co_argcount]
        self.defaults = func.__defaults__
        if self.defaults:
            n = len(self.defaults)
            self.params = list(varnames[:-n])
            self.options = list(varnames[-n:])
        else:
            self.params = list(varnames)
            self.options = []
            self.defaults = []

    def __repr__(self):
        return "<Command {}>".format(self)

    def __str__(self):
        return "{}({})".format(
            self.name,
            ", ".join(self.params + ["{}={!r}".format(name, self.defaults[i])
                                     for i, name in enumerate(self.options)]))


def command(s):
    def decorator(func):
        global known_commands
        name = func.__name__
        known_commands[name] = Command(func, s)
        @wraps(func)
        def func_wrapper(*args, **kwargs):
            c = known_commands[func.__name__]
            wallet = args[0].wallet
            network = args[0].network
            password = kwargs.get('password')
            if c.requires_network and network is None:
                raise Exception("Daemon offline")  # Same wording as in daemon.py.
            if c.requires_wallet and wallet is None:
                raise Exception("Wallet not loaded. Use 'electrum-sv daemon load_wallet'")
            if c.requires_password and password is None and wallet.storage.get('use_encryption') \
               and not kwargs.get("unsigned"):
                return {'error': 'Password required' }
            return func(*args, **kwargs)
        return func_wrapper
    return decorator


class Commands:

    def __init__(self, config, wallet, network, callback = None):
        self.config = config
        self.wallet = wallet
        self.network = network
        self._callback = callback

    def _run(self, method, *args, password_getter=None, **kwargs):
        # this wrapper is called from the python console
        cmd = known_commands[method]
        if cmd.requires_password and self.wallet.has_password():
            password = password_getter()
            if password is None:
                return
        else:
            password = None

        f = getattr(self, method)
        if cmd.requires_password:
            kwargs.update(password=password)
        result = f(*args, **kwargs)

        if self._callback:
            self._callback()
        return result

    @command('')
    def commands(self):
        """List of commands"""
        return ' '.join(sorted(known_commands.keys()))

    @command('')
    def help(self):
        # for the python console
        return sorted(known_commands.keys())

    @classmethod
    def _setconfig_normalize_value(cls, key, value):
        if key not in ('rpcuser', 'rpcpassword'):
            value = json_decode(value)
            try:
                value = ast.literal_eval(value)
            except:
                pass
        return value


param_descriptions = {
    'privkey': 'Private key. Type \'?\' to get a prompt.',
    'destination': 'Bitcoin SV address, contact or alias',
    'address': 'Bitcoin SV address',
    'seed': 'Seed phrase',
    'txid': 'Transaction ID',
    'pos': 'Position',
    'height': 'Block height',
    'tx': 'Serialized transaction (hexadecimal)',
    'key': 'Variable name',
    'pubkey': 'Public key',
    'message': 'Clear text message. Use quotes if it contains spaces.',
    'encrypted': 'Encrypted message',
    'amount': 'Amount to be sent (in BTC). Type \'!\' to send the maximum available.',
    'requested_amount': 'Requested amount (in BTC).',
    'outputs': 'list of ["address", amount]',
    'redeem_script': 'redeem script (hexadecimal)',
}

command_options = {
    'password':    ("-W", "Password"),
    'new_password':(None, "New Password"),
    'receiving':   (None, "Show only receiving addresses"),
    'change':      (None, "Show only change addresses"),
    'frozen':      (None, "Show only frozen addresses"),
    'unused':      (None, "Show only unused addresses"),
    'funded':      (None, "Show only funded addresses"),
    'balance':     ("-b", "Show the balances of listed addresses"),
    'labels':      ("-l", "Show the labels of listed addresses"),
    'nocheck':     (None, "Do not verify aliases"),
    'imax':        (None, "Maximum number of inputs"),
    'fee':         ("-f", "Transaction fee (in BTC)"),
    'from_addr':   ("-F", "Source address (must be a wallet address; "
                    "use sweep to spend from non-wallet address)."),
    'change_addr': ("-c", "Change address. Default is a spare address, or the source "
                    "address if it's not in the wallet"),
    'nbits':       (None, "Number of bits of entropy"),
    'language':    ("-L", "Default language for wordlist"),
    'privkey':     (None, "Private key. Set to '?' to get a prompt."),
    'unsigned':    ("-u", "Do not sign transaction"),
    'locktime':    (None, "Set locktime block number"),
    'domain':      ("-D", "List of addresses"),
    'memo':        ("-m", "Description of the request"),
    'expiration':  (None, "Time in seconds"),
    'timeout':     (None, "Timeout in seconds"),
    'force':       (None, "Create new address beyond gap limit, if no more addresses "
                    "are available."),
    'pending':     (None, "Show only pending requests."),
    'expired':     (None, "Show only expired requests."),
    'paid':        (None, "Show only paid requests."),
    'show_addresses': (None, "Show input and output addresses"),
    'show_fiat':   (None, "Show fiat value of transactions"),
    'year':        (None, "Show history for a given year"),
}


# don't use floats because of rounding errors
from .transaction import tx_from_str
json_loads = lambda x: json.loads(x, parse_float=lambda x: str(Decimal(x)))
arg_types = {
    'num': int,
    'nbits': int,
    'imax': int,
    'year': int,
    'tx': tx_from_str,
    'pubkeys': json_loads,
    'jsontx': json_loads,
    'inputs': json_loads,
    'outputs': json_loads,
    'fee': lambda x: str(Decimal(x)) if x is not None else None,
    'amount': lambda x: str(Decimal(x)) if x != '!' else '!',
    'locktime': int,
}

config_variables = {

    'addrequest': {
        'requests_dir': 'directory where a bip270 file will be written.',
        'url_rewrite': ('Parameters passed to str.replace(), in order to create the r= part '
                        'of bitcoin: URIs. Example: '
                        '\"(\'file:///var/www/\',\'https://electrum.org/\')\"'),
    },
    'listrequests':{
        'url_rewrite': ('Parameters passed to str.replace(), in order to create the r= part '
                        'of bitcoin: URIs. Example: '
                        '\"(\'file:///var/www/\',\'https://electrum.org/\')\"'),
    }
}

def set_default_subparser(self, name, args=None):
    """see http://stackoverflow.com/questions/5176691"""
    subparser_found = False
    for arg in sys.argv[1:]:
        if arg in ['-h', '--help']:  # global help if no subparser
            break
    else:
        for x in self._subparsers._actions:
            if not isinstance(x, argparse._SubParsersAction):
                continue
            for sp_name in x._name_parser_map.keys():
                if sp_name in sys.argv[1:]:
                    subparser_found = True
        if not subparser_found:
            # insert default in first position, this implies no
            # global options without a sub_parsers specified
            if args is None:
                sys.argv.insert(1, name)
            else:
                args.insert(0, name)

argparse.ArgumentParser.set_default_subparser = set_default_subparser


# workaround https://bugs.python.org/issue23058
# see https://github.com/nickstenning/honcho/pull/121

def subparser_call(self, parser, namespace, values, option_string=None):
    from argparse import ArgumentError, SUPPRESS, _UNRECOGNIZED_ARGS_ATTR
    parser_name = values[0]
    arg_strings = values[1:]
    # set the parser name if requested
    if self.dest is not SUPPRESS:
        setattr(namespace, self.dest, parser_name)
    # select the parser
    try:
        parser = self._name_parser_map[parser_name]
    except KeyError:
        tup = parser_name, ', '.join(self._name_parser_map)
        msg = _('unknown parser {!r} (choices: {})').format(*tup)
        raise ArgumentError(self, msg)
    # parse all the remaining options into the namespace
    # store any unrecognized options on the object, so that the top
    # level parser can decide what to do with them
    namespace, arg_strings = parser.parse_known_args(arg_strings, namespace)
    if arg_strings:
        vars(namespace).setdefault(_UNRECOGNIZED_ARGS_ATTR, [])
        getattr(namespace, _UNRECOGNIZED_ARGS_ATTR).extend(arg_strings)

argparse._SubParsersAction.__call__ = subparser_call


def add_network_options(parser):
    parser.add_argument("-1", "--oneserver", action="store_true", dest="oneserver",
                        default=False, help="connect to one server only")
    parser.add_argument("-s", "--server", dest="server", default=None,
                        help="set server host:port:protocol, where protocol is either "
                        "t (tcp) or s (ssl)")
    parser.add_argument("-p", "--proxy", dest="proxy", default=None,
                        help="set proxy [type:]host[:port], where type is socks4 or socks5")

def add_global_options(parser):
    group = parser.add_argument_group('global options')
    group.add_argument("-v", "--verbose", action="store", dest="verbose",
                       const='info', default='warning', nargs='?',
                       choices = ('debug', 'info', 'warning', 'error'),
                       help="Set logging verbosity")
    group.add_argument("-D", "--dir", dest="electrum_sv_path", help="ElectrumSV directory")
    group.add_argument("-P", "--portable", action="store_true", dest="portable", default=False,
                       help="Use local 'electrum_data' directory")
    group.add_argument("-w", "--wallet", dest="wallet_path", help="wallet path")
    group.add_argument("-wp", "--walletpassword", dest="wallet_password", default=None,
                       help="Supply wallet password")
    group.add_argument("--testnet", action="store_true", dest="testnet", default=False,
                       help="Use Testnet")
    group.add_argument("--scaling-testnet", action="store_true", dest="scalingtestnet",
                       default=False, help="Use Scaling Testnet")
    group.add_argument("--file-logging", action="store_true", dest="file_logging", default=False,
                       help="Redirect logging to log file")

def get_parser():
    # create main parser
    parser = argparse.ArgumentParser(
        epilog="Run 'electrum-sv help <command>' to see the help for a command")
    add_global_options(parser)
    subparsers = parser.add_subparsers(dest='cmd', metavar='<command>')
    # gui
    parser_gui = subparsers.add_parser('gui',
                                       description="Run Electrum's Graphical User Interface.",
                                       help="Run GUI (default)")
    parser_gui.add_argument("url", nargs='?', default=None, help="bitcoin URI (or bip270 file)")
    parser_gui.add_argument("-g", "--gui", dest="gui", help="select graphical user interface",
                            choices=['qt'])
    parser_gui.add_argument("-o", "--offline", action="store_true", dest="offline", default=False,
                            help="Run offline")
    parser_gui.add_argument("-m", action="store_true", dest="hide_gui", default=False,
                            help="hide GUI on startup")
    parser_gui.add_argument("-L", "--lang", dest="language", default=None,
                            help="default language used in GUI")
    add_network_options(parser_gui)
    add_global_options(parser_gui)
    # daemon
    parser_daemon = subparsers.add_parser('daemon', help="Run Daemon")
    parser_daemon.add_argument("subcommand", choices=['start', 'status', 'stop',
                                                      'load_wallet', 'close_wallet'], nargs='?')
    parser_daemon.add_argument("-dapp", "--daemon-app-module", dest="daemon_app_module",
        help="Run the daemon control app from the given module")
    #parser_daemon.set_defaults(func=run_daemon)
    add_network_options(parser_daemon)
    add_global_options(parser_daemon)
    # commands
    for cmdname in sorted(known_commands.keys()):
        cmd = known_commands[cmdname]
        p = subparsers.add_parser(cmdname, help=cmd.help, description=cmd.description)
        add_global_options(p)
        if cmdname == 'restore':
            p.add_argument("-o", "--offline", action="store_true", dest="offline", default=False,
                           help="Run offline")
        for optname, default in zip(cmd.options, cmd.defaults):
            a, help = command_options[optname]
            b = '--' + optname
            action = "store_true" if type(default) is bool else 'store'
            args = (a, b) if a else (b,)
            if action == 'store':
                _type = arg_types.get(optname, str)
                p.add_argument(*args, dest=optname, action=action, default=default,
                               help=help, type=_type)
            else:
                p.add_argument(*args, dest=optname, action=action, default=default, help=help)

        for param in cmd.params:
            h = param_descriptions.get(param, '')
            _type = arg_types.get(param, str)
            p.add_argument(param, help=h, type=_type)

        cvh = config_variables.get(cmdname)
        if cvh:
            group = p.add_argument_group('configuration variables',
                                         '(set with setconfig/getconfig)')
            for k, v in cvh.items():
                group.add_argument(k, nargs='?', help=v)

    # 'gui' is the default command
    parser.set_default_subparser('gui')
    return parser
