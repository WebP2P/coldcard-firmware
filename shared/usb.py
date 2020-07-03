# (c) Copyright 2018 by Coinkite Inc. This file is part of Coldcard <coldcardwallet.com>
# and is covered by GPLv3 license found in COPYING.
#
# usb.py - USB related things
#
import ckcc, pyb, callgate, sys, ux, tcc, stash
from uasyncio import sleep_ms, IORead
from public_constants import MAX_MSG_LEN, MAX_TXN_LEN, MAX_BLK_LEN, MAX_UPLOAD_LEN, AFC_SCRIPT
from public_constants import STXN_FLAGS_MASK
from ustruct import pack, unpack_from
from ubinascii import hexlify as b2a_hex
from ckcc import rng_bytes, watchpoint, is_simulator
import uselect as select
from utils import problem_file_line
from version import has_fatram
from exceptions import FramingError, CCBusyError, HSMDenied

# Unofficial, unpermissioned... numbers
COINKITE_VID = 0xd13e
CKCC_PID     = 0xcc10

# Based on the U2F descriptor:
# see <https://fidoalliance.org/specs/fido-u2f-v1.0-ps-20141009/fido-u2f-hid-protocol-ps-20141009.html>
# however, we don't want to be detected as a U2F device, because we 
# don't support that protocol, and we don't want web browsers and such
# trying to speak U2F/HID/USB protocol at us. This descriptor is just to
# keep the HID class drivers happy, we will detect based on VID/PID.
#
hid_descp = bytes([
            0x06, 0xcc, 0x10,  # USAGE_PAGE (CC10 = Coldcard v1.0)
            0x09, 0x01,        # USAGE (0x01)
            0xa1, 0x01,        # COLLECTION (Application)
            0x09, 0x20,        # USAGE (Input Report Data)
            0x15, 0x00,        # LOGICAL_MINIMUM (0)
            0x26, 0xff, 0x00,  # LOGICAL_MAXIMUM (255)
            0x75, 0x08,        # REPORT_SIZE (8)
            0x95, 0x40,        # REPORT_COUNT (64)
            0x81, 0x02,        # INPUT (Data,Var,Abs)
            0x09, 0x21,        # USAGE (Output Report Data)
            0x15, 0x00,        # LOGICAL_MINIMUM (0)
            0x26, 0xff, 0x00,  # LOGICAL_MAXIMUM (255)
            0x75, 0x08,        # REPORT_SIZE (8)
            0x95, 0x40,        # REPORT_COUNT (64)
            0x91, 0x02,        # OUTPUT (Data,Var,Abs)
            0xc0,              # END_COLLECTION
        ])

# Only these whitelisted USB commands are allowed once we enter HSM mode.
# NOTE: 'robo' here would allow firmware changes during HSM mode!
HSM_WHITELIST = frozenset({
    'logo', 'ping', 'vers',     # harmless/boring
    'upld', 'sha2', 'dwld', 'stxn',     # up/download/sign PSBT needed
    'mitm','ncry',              # maybe limited by policy tho
    'smsg',                     # limited by policy
    'blkc', 'hsts',             # report status values
    'stok', 'smok',             # completion check: sign txn or msg
    'xpub', 'msck',             # quick status checks
    'p2sh', 'show',             # limited by HSM policy
    'user',                     # auth HSM user, other user cmds not allowed
    'gslr',                     # read storage locker; hsm mode only, limited usage
})



# singleton instance of USBHandler()
handler = None

def enable_usb(loop, repl_enable=False):
    # start it.
    cur = pyb.usb_mode()

    # allow/block REPL access
    ckcc.vcp_enabled(repl_enable)

    if cur:
        # We can't change it on the fly; must be disabled before here
        print("USB already enabled")
    else:
        # subclass, protocol, max packet length, polling interval, report descriptor
        hid_info = (0x0, 0x0, 64, 5, hid_descp )
        try:
            pyb.usb_mode('VCP+HID', vid=COINKITE_VID, pid=CKCC_PID, hid=hid_info)
        except:
            assert False, 'bad usb mode'
            return

    global handler
    if loop and not handler:
        handler = USBHandler()
        loop.create_task(handler.usb_hid_recv())

def is_vcp_active():
    # VCP = Virtual Comm Port
    en = ckcc.vcp_enabled(None)
    cur = pyb.usb_mode()

    return cur and ('VCP' in cur) and en

class USBHandler:
    def __init__(self):
        self.dev = pyb.USB_HID()

        # We keep a running hash over whatever has been uploaded
        # - reset at offset zero, can be read back anytime
        self.file_checksum = tcc.sha256()

        # handle simulator
        self.blockable = getattr(self.dev, 'pipe', self.dev)

        #self.msg = bytearray(MAX_MSG_LEN)
        from sram2 import usb_buf
        self.msg = usb_buf
        assert len(self.msg) == MAX_MSG_LEN

        self.encrypted_req = False

        # these will be tcc.AES objects later
        self.encrypt = None
        self.decrypt = None

    def get_packet(self):
        # read next packet (64 bytes) waiting on the wire. Unframe it and return
        # active part of packet, flags associated.
        buf = self.dev.recv(64, timeout=5000)

        if not buf:
            raise FramingError('timeout')
        elif len(buf) < 64:
            raise FramingError('short')
        elif len(buf) > 64:
            raise FramingError('long')

        # first byte gives us the actual size, status
        # all illegal combos here may become special messages someday
        flag = buf[0]
        is_last  = bool(flag & 0x80)
        len_here = int(flag & 0x3f)
        is_encrypted = bool(flag & 0x40)

        return buf[1:1+len_here], is_last, is_encrypted

    async def usb_hid_recv(self):
        # blocks and builds up a full-length command packet in memory
        # - calls self.handle() once complete msg on hand
        msg_len = 0

        while 1:
            yield IORead(self.blockable)

            try:
                here, is_last, is_encrypted = self.get_packet()

                #print('Rx[%d]' % len(here))
                if here:
                    lh = len(here)
                    if msg_len+lh > MAX_MSG_LEN:
                        raise FramingError('xlong')

                    self.msg[msg_len:msg_len + lh] = here
                    msg_len += lh
                else:
                    # treat zero-length packets as a reset request
                    # do not echo anything back on link.. used to resync connection
                    msg_len = 0
                    continue

                if not is_last:
                    # need more content
                    continue

                if not(4 <= msg_len <= MAX_MSG_LEN):
                    raise FramingError('badsz')

                if is_encrypted:
                    if self.decrypt is None:
                        raise FramingError('no key')

                    self.encrypted_req = True
                    self.decrypt_inplace(msg_len)
                else:
                    self.encrypted_req = False

                # process request
                try:
                    # this saves memory over a simple slice (confirmed)
                    args = memoryview(self.msg)[4:msg_len]
                    resp = await self.handle(self.msg[0:4], args)
                    msg_len = 0
                except CCBusyError:
                    # auth UX is doing something else
                    resp = b'busy'
                    msg_len = 0
                except HSMDenied:
                    resp = b'err_Not allowed in HSM mode'
                    msg_len = 0
                except (ValueError, AssertionError) as exc:
                    # some limited invalid args feedback
                    #print("USB request caused assert: ", end='')
                    #sys.print_exception(exc)
                    msg = str(exc)
                    if not msg:
                        msg = 'Assertion ' + problem_file_line(exc)
                    resp = b'err_' + msg.encode()[0:80]
                    msg_len = 0
                except Exception as exc:
                    # catch bugs and fuzzing too
                    print("USB request caused this: ", end='')
                    sys.print_exception(exc)
                    resp = b'err_Confused ' + problem_file_line(exc)
                    msg_len = 0

                # aways send a reply if they get this far
                await self.send_response(resp)

            except FramingError as exc:
                reason = exc.args[0]
                print("Framing: %s" % reason)
                self.framing_error(reason)
                msg_len = 0

            except BaseException as exc:
                # recover from general issues/keep going
                print("USB!")
                sys.print_exception(exc)
                msg_len = 0

    def decrypt_inplace(self, msg_len):
        # self.msg is encrypted. decode it in place
        # - seems dangerous to use memview here, but works
        # - some memory alloc still happens here tho; probably in return of decrypt.update
        self.msg[0:msg_len] = self.decrypt.update(memoryview(self.msg)[0:msg_len])

    def encrypt_response(self, msg):
        # encrypt what we'll send to desktop

        return self.encrypt.update(msg)

    async def send_response(self, resp):
        # send a python object as the response
        # - we know how to encode a few things, or send binary
        # - sadly we cannot stream here because we cannot subclass streams
        # - cannot reuse rx buffer either!

        # handle simple types here

        if isinstance(resp, (bytes, bytearray)):
            # preformated
            assert len(resp) >= 4
        elif resp is None:
            resp = b'okay'
        elif isinstance(resp, int):
            resp = pack('<4sI', 'int1', resp)
        else:
            print("Unknown resp: " + repr(resp))
            raise NotImplementedError()

        assert len(resp) >= 4

        msg = bytearray(64)

        if self.encrypt and self.encrypted_req:
            resp = self.encrypt_response(resp)
            final_flag = 0x80 | 0x40
        else:
            final_flag = 0x80

        pos = 0
        left = len(resp)
        while left:
            # sent up to 63 bytes per packet
            here = min(left, 63)
            msg[0] = here
            msg[1:1+here] = resp[pos:pos+here]
            if here == left:
                # no more to come
                assert 0 <= here < 64
                msg[0] |= final_flag

            left -= here
            pos += here

            for retries in range(100):
                chk = self.dev.send(msg)
                if chk == 64: break

                # Host may not have read previous value yet, so might need
                # to wait for it. Data loss possible here, but also the
                # host may stop reading the EP forever, so not our fault.
                # Let other stuff run during this delay.
                await sleep_ms(10)

    def framing_error(self, why):
        # send error about framing, and recover
        self.dev.send(b'%cfram%-59s' % (4+len(why), why))


    async def handle(self, cmd, args):
        # Dispatch incoming message, and provide reply.
        from main import hsm_active, is_devmode

        try:
            cmd = bytes(cmd).decode()
        except:
            raise FramingError('decode')

        if cmd[0].isupper() and (is_simulator() or is_devmode):
            # special hacky commands to support testing w/ the simulator
            try:
                from usb_test_commands import do_usb_command
                return do_usb_command(cmd, args)
            except: 
                raise
                pass

        if hsm_active:
            # only a few commands are allowed during HSM mode
            if cmd not in HSM_WHITELIST:
                raise HSMDenied

        if cmd == 'dfu_':
            # only useful in factory, undocumented.
            return self.call_after(callgate.enter_dfu)

        if cmd == 'rebo':
            import machine
            return self.call_after(machine.reset)

        if cmd == 'logo':
            from utils import clean_shutdown
            return self.call_after(clean_shutdown)

        if cmd == 'ping':
            return b'biny' + args

        if cmd == 'upld':
            offset, total_size = unpack_from('<II', args)
            data = memoryview(args)[4+4:]

            return await self.handle_upload(offset, total_size, data)

        if cmd == 'dwld':
            offset, length, fileno = unpack_from('<III', args)
            return await self.handle_download(offset, length, fileno)

        if cmd == 'ncry':
            version, his_pubkey = unpack_from('<I64s', args)

            return self.handle_crypto_setup(version, his_pubkey)

        if cmd == 'vers':
            from version import get_mpy_version, hw_label
            from callgate import get_bl_version

            # Returning: date, version(human), bootloader version, full date version
            # BUT: be ready for additions!
            rv = list(get_mpy_version())
            rv.insert(2, get_bl_version()[0])
            rv.append(hw_label)

            return b'asci' + ('\n'.join(rv)).encode()

        if cmd == 'sha2':
            return b'biny' + self.file_checksum.digest()

        if cmd == 'xpub':
            assert self.encrypted_req, 'must encrypt'
            return self.handle_xpub(args)

        if cmd == 'mitm':
            assert self.encrypted_req, 'must encrypt'
            return await self.handle_mitm_check()

        if cmd == 'smsg':
            # sign message
            addr_fmt, len_subpath, len_msg = unpack_from('<III', args)
            subpath = args[12:12+len_subpath]
            msg = args[12+len_subpath:]
            assert len(msg) == len_msg, "badlen"

            from auth import sign_msg
            sign_msg(msg, subpath, addr_fmt)
            return None

        if cmd == 'p2sh':
            # show P2SH (probably multisig) address on screen (also provides it back)
            # - must provide redeem script, and list of [xfp+path]
            from auth import start_show_p2sh_address

            if hsm_active and not hsm_active.approve_address_share(is_p2sh=True):
                raise HSMDenied

            # new multsig goodness, needs mapping from xfp->path and M values
            addr_fmt, M, N, script_len = unpack_from('<IBBH', args)

            assert addr_fmt & AFC_SCRIPT
            assert 1 <= M <= N <= 20
            assert 30 <= script_len <= 520

            offset = 8
            witdeem_script = args[offset:offset+script_len]
            offset += script_len

            assert len(witdeem_script) == script_len

            xfp_paths = []
            for i in range(N):
                ln = args[offset]
                assert 1 <= ln <= 16, 'badlen'
                xfp_paths.append(unpack_from('<%dI' % ln, args, offset+1))
                offset += (ln*4) + 1

            assert offset == len(args)

            return b'asci' + start_show_p2sh_address(M, N, addr_fmt, xfp_paths,
                                                        witdeem_script)

        if cmd == 'show':
            # simple cases, older code: text subpath
            from auth import start_show_address

            addr_fmt, = unpack_from('<I', args)
            assert not (addr_fmt & AFC_SCRIPT)

            return b'asci' + start_show_address(addr_fmt, subpath=args[4:])

        if cmd == 'enrl':
            # Enroll new xpubkey to be involved in multisigs.
            # - text config file must already be uploaded

            file_len, file_sha = unpack_from('<I32s', args)
            if file_sha != self.file_checksum.digest():
                return b'err_Checksum'
            assert 100 < file_len <= (20*200), "badlen"

            # Start an UX interaction, return immediately here
            from auth import maybe_enroll_xpub
            maybe_enroll_xpub(sf_len=file_len, ux_reset=True)

            return None

        if cmd == 'msck':
            # Quick check to test if we have a wallet already installed.
            from multisig import MultisigWallet
            M, N, xfp_xor = unpack_from('<3I', args)

            return int(MultisigWallet.quick_check(M, N, xfp_xor))

        if cmd == 'stxn':
            # sign transaction
            txn_len, flags, txn_sha = unpack_from('<II32s', args)
            if txn_sha != self.file_checksum.digest():
                return b'err_Checksum'

            assert 50 < txn_len <= MAX_TXN_LEN, "bad txn len"

            from auth import sign_transaction
            sign_transaction(txn_len, (flags & STXN_FLAGS_MASK), txn_sha)
            return None

        if cmd == 'stok' or cmd == 'bkok' or cmd == 'smok' or cmd == 'pwok':
            # Have we finished (whatever) the transaction,
            # which needed user approval? If so, provide result.
            from auth import UserAuthorizedAction

            req = UserAuthorizedAction.active_request
            if not req:
                return b'err_No active request'

            if req.refused:
                UserAuthorizedAction.cleanup()
                return b'refu'

            if req.failed:
                rv = b'err_' + req.failed.encode()
                UserAuthorizedAction.cleanup()
                return rv

            if not req.result:
                # STILL waiting on user
                return None


            if cmd == 'pwok':
                # return new root xpub
                xpub = req.result
                UserAuthorizedAction.cleanup()
                return b'asci' + bytes(xpub, 'ascii')
            elif cmd == 'smok':
                # signed message done: just give them the signature
                addr, sig = req.address, req.result
                UserAuthorizedAction.cleanup()
                return pack('<4sI', 'smrx', len(addr)) + addr.encode() + sig
            else:
                # generic file response
                resp_len, sha = req.result
                UserAuthorizedAction.cleanup()
                return pack('<4sI32s', 'strx', resp_len, sha)

        if cmd == 'pass':
            # bip39 passphrase provided, maybe use it if authorized
            assert self.encrypted_req, 'must encrypt'
            from auth import start_bip39_passphrase
            from main import settings

            assert settings.get('words', True), 'no seed'
            assert len(args) < 400, 'too long'
            pw = str(args, 'utf8')
            assert len(pw) < 100, 'too long'

            return start_bip39_passphrase(pw)

        if cmd == 'back':
            # start backup: asks user, takes long time.
            from auth import start_remote_backup
            return start_remote_backup()

        if cmd == 'blkc':
            # report which blockchain we are configured for
            from chains import current_chain
            chain = current_chain()
            return b'asci' + chain.ctype

        if cmd == 'bagi':
            return self.handle_bag_number(args)

        if has_fatram:
            # HSM and user-related  features only larger-memory Mk3

            if cmd == 'hsms':
                # HSM mode "start" -- requires user approval
                if args:
                    file_len, file_sha = unpack_from('<I32s', args)
                    if file_sha != self.file_checksum.digest():
                        return b'err_Checksum'
                    assert 2 <= file_len <= (200*1000), "badlen"
                else:
                    file_len = 0

                # Start an UX interaction but return (mostly) immediately here
                from hsm_ux import start_hsm_approval
                await start_hsm_approval(sf_len=file_len, usb_mode=True)

                return None

            if cmd == 'hsts':
                # can always query HSM mode
                from hsm import hsm_status_report
                import ujson
                return b'asci' + ujson.dumps(hsm_status_report())

            if cmd == 'gslr':
                # get the value held in the Storage Locker
                assert hsm_active, 'need hsm'
                return b'biny' + hsm_active.fetch_storage_locker()


            # User Mgmt
            if cmd == 'nwur':     # new user
                from users import Users
                auth_mode, ul, sl = unpack_from('<BBB', args)
                username = bytes(args[3:3+ul]).decode('ascii')
                secret = bytes(args[3+ul:3+ul+sl])

                return b'asci' + Users.create(username, auth_mode, secret).encode('ascii')

            if cmd == 'rmur':     # delete user
                from users import Users
                ul, = unpack_from('<B', args)
                username = bytes(args[1:1+ul]).decode('ascii')

                return Users.delete(username)

            if cmd == 'user':       # auth user (HSM mode)
                from users import Users
                totp_time, ul, tl = unpack_from('<IBB', args)
                username = bytes(args[6:6+ul]).decode('ascii')
                token = bytes(args[6+ul:6+ul+tl])

                if hsm_active:
                    # just queues these details, can't be checked until PSBT on-hand
                    hsm_active.usb_auth_user(username, token, totp_time)
                    return None
                else:
                    # dryrun/testing purposes: validate only, doesn't unlock nothing
                    return b'asci' + Users.auth_okay(username, token, totp_time).encode('ascii')

        print("USB garbage: %s +[%d]" % (cmd, len(args)))

        return b'err_Unknown cmd'


    def call_after(self, func, *args):
        from main import loop

        async def doit():
            # we want to provide nice response before dying
            await sleep_ms(500)
            func(*args)

        loop.create_task(doit())

        return None

    def handle_crypto_setup(self, version, his_pubkey):
        # pick a one-time key pair for myself, and return the pubkey for that
        # determine what the session key will be for this connection
        assert version == 0x1
        assert len(his_pubkey) == 64

        # pick a random key pair, just for this session
        my_key = tcc.secp256k1.generate_secret()
        my_pubkey = tcc.secp256k1.publickey(my_key, False)

        #print('my pubkey = ' + str(b2a_hex(my_pubkey)))
        #print('his pubkey = ' + str(b2a_hex(his_pubkey)))

        pt = tcc.secp256k1.multiply(my_key, b'\x04' + his_pubkey)
        #assert pt[0] == 4
        self.session_key = tcc.sha256(pt[1:]).digest()

        #print("session = " + str(b2a_hex(self.session_key)))

        # Would be nice to have nonce in addition to the counter, but
        # library not ready for that, and also harder on the desktop side.
        self.encrypt = tcc.AES(tcc.AES.CTR | tcc.AES.Encrypt, self.session_key)
        self.decrypt = tcc.AES(tcc.AES.CTR | tcc.AES.Decrypt, self.session_key)

        from main import settings
        xfp = settings.get('xfp', 0)
        xpub = settings.get('xpub', '')

        #assert my_pubkey[0] == 0x04
        return b'mypb' + my_pubkey[1:] + pack('<II', xfp, len(xpub)) +  xpub

    async def handle_mitm_check(self):
        # Sign the current session key using our master (bitcoin) key.
        # - proves our identity and that no-one is between 

        # Rate limit and fuzz timing in case we have timing sensitivity
        await sleep_ms(250 + tcc.random.uniform(1000))

        with stash.SensitiveValues() as sv:
            pk = sv.node.private_key()
            sv.register(pk)

            signature = tcc.secp256k1.sign(pk, self.session_key)

            assert len(signature) == 65

        return b'biny' + signature


    async def handle_download(self, offset, length, file_number):
        # let them read from where we store the signed txn
        # - filenumber can be 0 or 1: uploaded txn, or result
        from main import sf

        # limiting memory use here, should be MAX_BLK_LEN really
        length = min(length, MAX_BLK_LEN)

        assert 0 <= file_number < 2, 'bad fnum'
        assert 0 <= offset <= MAX_TXN_LEN, "bad offset"
        assert 1 <= length, 'len'

        # maintain a running SHA256 over what's sent
        if offset == 0:
            self.file_checksum = tcc.sha256()

        pos = (MAX_TXN_LEN * file_number) + offset

        resp = bytearray(4 + length)
        resp[0:4] = b'biny'
        sf.read(pos, memoryview(resp)[4:])

        self.file_checksum.update(memoryview(resp)[4:])

        return resp

    async def handle_upload(self, offset, total_size, data):
        from main import dis, sf, hsm_active
        from utils import check_firmware_hdr
        from sigheader import FW_HEADER_OFFSET, FW_HEADER_SIZE

        # maintain a running SHA256 over what's received
        if offset == 0:
            self.file_checksum = tcc.sha256()

        assert offset % 256 == 0, 'alignment'
        assert offset+len(data) <= total_size <= MAX_UPLOAD_LEN, 'long'

        if hsm_active:
            # additional restrictions in HSM mode
            assert offset+len(data) <= total_size <= MAX_TXN_LEN, 'psbt'
            if offset == 0:
                assert data[0:5] == b'psbt\xff', 'psbt'

        for pos in range(offset, offset+len(data), 256):
            if pos % 4096 == 0:
                # erase here
                dis.fullscreen("Receiving...", offset/total_size)

                sf.sector_erase(pos)

                while sf.is_busy():
                    await sleep_ms(10)

            # write up to 256 bytes
            here = data[pos-offset:pos-offset+256]

            self.file_checksum.update(here)

            # Very special case for firmware upgrades: intercept and modify
            # header contents on the fly, and also fail faster if wouldn't work
            # on this specific hardware.
            # - workaround: ckcc-protocol upgrade process understates the file
            #   length and appends hdr, but that's kinda a bug, so support both
            if (pos == (FW_HEADER_OFFSET & ~255) 
                or pos == (total_size - FW_HEADER_SIZE) or pos == total_size):

                prob = check_firmware_hdr(memoryview(here)[-128:], None, bad_magic_ok=True)
                if prob:
                    raise ValueError(prob)

            sf.write(pos, here)

            # full page write: 0.6 to 3ms
            while sf.is_busy():
                await sleep_ms(1)


        if offset+len(data) >= total_size and not hsm_active:
            # probably done
            dis.progress_bar_show(1.0)
            ux.restore_menu()

        return offset

    def handle_xpub(self, subpath):
        # Share the xpub for the indicated subpath. Expects
        # a text string which is the path derivation.

        # TODO: might not have a privkey yet

        from chains import current_chain
        from utils import cleanup_deriv_path

        subpath = cleanup_deriv_path(subpath)

        from main import hsm_active
        if hsm_active and not hsm_active.approve_xpub_share(subpath):
            raise HSMDenied

        chain = current_chain()

        with stash.SensitiveValues() as sv:
            node = sv.derive_path(subpath)

            xpub = chain.serialize_public(node)

            return b'asci' + xpub.encode()

    def handle_bag_number(self, bag_num):
        import version, callgate
        from main import dis, pa, is_devmode, settings

        if version.is_factory_mode and bag_num:
            # check state first
            assert settings.get('tested', False)
            assert pa.is_blank()
            assert 8 <= len(bag_num) < 32

            # do the change
            failed = callgate.set_bag_number(bag_num)
            assert not failed

            callgate.set_rdp_level(2 if not is_devmode else 0)
            pa.greenlight_firmware()
            dis.fullscreen(bytes(bag_num).decode())

            self.call_after(callgate.show_logout, 1)

        # always report the existing/new value
        val = callgate.get_bag_number() or b''

        return b'asci' + val

# EOF 
