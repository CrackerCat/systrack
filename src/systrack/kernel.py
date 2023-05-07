import re
import sys
import logging
import struct
import atexit
from pathlib import Path
from time import monotonic
from os import sched_getaffinity
from operator import itemgetter, attrgetter
from collections import defaultdict, Counter
from itertools import zip_longest
from typing import Tuple, List, Iterator, Union, Any

from .elf import ELF, Symbol, Section
from .arch import Arch, arch_from_name, arch_from_vmlinux
from .syscall import Syscall, common_syscall_symbol_prefixes
from .utils import run_command, ensure_command, VersionedDict, high_verbosity
from .utils import maybe_rel, noprefix
from .location import extract_syscall_locations
from .signature import extract_syscall_signatures
from .kconfig import edit_config, edit_config_check_deps
from .kconfig import kconfig_more_syscalls, kconfig_debugging
from .kconfig import kconfig_compatibility, kconfig_syscall_deps
from .type_hints import KernelVersion

class KernelVersionError(RuntimeError):
	pass

class KernelArchError(RuntimeError):
	pass

class KernelWithoutSymbolsError(RuntimeError):
	pass

class KernelMultiABIError(RuntimeError):
	pass

class Kernel:
	__vmlinux         = None
	__version         = None
	__version_source  = None
	__syscalls        = None
	__backup_makefile = None
	__long_size       = None
	__long_pack_fmt   = None

	def __init__(self, arch_name: str = None, vmlinux: Path = None,
			kdir: Path = None, outdir: Path = None, rdir: Path = None,
			toolchain_prefix: str = None):
		if not kdir and not vmlinux:
			raise ValueError('at least one of vmlinux or kdir is needed')
		if arch_name is None and vmlinux is None:
			raise ValueError('need vmlinux to determine arch if not supplied')

		self.kdir             = kdir
		self.outdir           = outdir
		self.rdir             = rdir
		self.vmlinux          = ELF(vmlinux) if vmlinux else None
		self.arch_name        = arch_name
		self.toolchain_prefix = toolchain_prefix

		if self.vmlinux and not self.vmlinux.symbols:
			raise KernelWithoutSymbolsError('Provided vmlinux ELF has no symbols')

		if self.arch_name is None:
			m = arch_from_vmlinux(self.vmlinux)
			if m is None:
				raise KernelArchError('Failed to detect kernel architecture/ABI')

			arch_class, bits32, abis = m
			if len(abis) > 1:
				raise KernelMultiABIError('Multiple ABIs supported, need to '
					'select one', arch_class, abis)

			self.arch = arch_class(self.version, abis[0], bits32)
		else:
			self.arch = arch_from_name(self.arch_name, self.version)

		if self.vmlinux:
			if not self.arch.matches(self.vmlinux):
				raise KernelArchError(f'Architecture {arch_name} does not '
					'match provided vmlinux')

			self.__long_size     = (8, 4)[self.vmlinux.bits32]
			self.__long_pack_fmt = '<>'[self.vmlinux.big_endian] + 'QL'[self.vmlinux.bits32]

	@staticmethod
	def version_from_str(s: str) -> KernelVersion:
		m = re.match(r'(\d+)\.(\d+)(\.(\d+))?', s)
		if not m:
			return None

		a, b, c = int(m.group(1)), int(m.group(2)), m.group(4)
		return (a, b) if c is None else (a, b, int(c))

	@staticmethod
	def version_from_banner(banner: Union[str,bytes]) -> KernelVersion:
		if isinstance(banner, bytes):
			banner = banner.decode()

		if not banner.startswith('Linux version '):
			return None
		return Kernel.version_from_str(banner[14:])

	def __version_from_vmlinux(self) -> KernelVersion:
		banner = self.vmlinux.symbols.get('linux_banner')
		if banner is None:
			return None

		if banner.size:
			banner = self.vmlinux.read_symbol(banner)
		else:
			banner = self.vmlinux.vaddr_read_string(banner.vaddr)

		return self.version_from_banner(banner)

	def __version_from_make(self) -> KernelVersion:
		v = ensure_command('make kernelversion', self.kdir)
		return self.version_from_str(v)

	@property
	def version(self) -> KernelVersion:
		if self.__version is None:
			if self.vmlinux:
				self.__version = self.__version_from_vmlinux()
				self.__version_source = 'vmlinux'
			elif self.kdir:
				# This could in theory be tried even if __version_from_vmlinux()
				# fails... but if that fails there are probably bigger problems.
				self.__version = self.__version_from_make()
				self.__version_source = 'make'

		if self.__version is None:
			raise KernelVersionError('unable to determine kernel version')
		return self.__version

	@property
	def version_str(self) -> str:
		return '.'.join(map(str, self.version)) + f' (from {self.__version_source})'

	@property
	def version_tag(self) -> str:
		a, b, c = self.version
		if c == 0:
			return f'v{a}.{b}'
		return f'v{a}.{b}.{c}'

	@property
	def version_source(self) -> str:
		if self.__version_source or self.version:
			return self.__version_source
		return None

	@property
	def can_extract_location_info(self):
		return self.vmlinux.has_debug_info

	@property
	def can_extract_signature_info(self):
		return (
			'__start_syscalls_metadata' in self.vmlinux.symbols
			or self.vmlinux.has_debug_info
		)

	@property
	def syscalls(self) -> List[Syscall]:
		if self.__syscalls is None:
			self.__syscalls = self.__extract_syscalls()
		return self.__syscalls

	def __rel(self, path: Path) -> Path:
		return maybe_rel(path, self.kdir)

	def __iter_unpack_vmlinux(self, fmt: str, off: int, size: int = None) -> Iterator[Tuple[Any, ...]]:
		f = self.vmlinux.file
		assert f.seek(off) == off

		if size is None:
			chunk_size = struct.calcsize(fmt)
			while 1:
				yield struct.unpack(fmt, f.read(chunk_size))
		else:
			yield from struct.iter_unpack(fmt, f.read(size))

	def __iter_unpack_vmlinux_long(self, off: int, size: int = None) -> Iterator[int]:
		yield from map(itemgetter(0), self.__iter_unpack_vmlinux(self.__long_pack_fmt, off, size))

	def __unpack_syscall_table(self, tbl: Symbol, target_section: Section) -> List[int]:
		tbl_file_off = self.vmlinux.vaddr_to_file_offset(tbl.vaddr)

		# This is the section we would like the function pointers to point to,
		# we'll warn or halt in case we find fptrs pointing outside
		vstart = target_section.vaddr
		vend   = vstart + target_section.size

		if tbl.size > 0x80:
			logging.info('Syscall table (%s) is %d bytes, %d entries', tbl.name,
				tbl.size, tbl.size // self.__long_size)

			vaddrs = list(self.__iter_unpack_vmlinux_long(tbl_file_off, tbl.size))

			# Sanity check: ensure all vaddrs are within the target section
			for idx, vaddr in enumerate(vaddrs):
				if not (vstart <= vaddr < vend):
					logging.warn('Virtual address 0x%x idx %d is outside %s: '
						'something is off!', vaddr, tbl.name, idx, target_section.name)
		else:
			# Apparently on some archs (e.g. MIPS, PPC) the syscall table symbol
			# can have size 0. In this case we'll just warn the user and keep
			# extracting vaddrs as long as they are valid, stopping at the first
			# invalid one or at the next symbol we encounter.
			logging.warn('Syscall table (%s) has bad size (%d), doing my best '
				'to figure out when to stop', tbl.name, tbl.size)

			cur_idx_vaddr = tbl.vaddr
			boundaries = filter(lambda x: x.type != 'FUNC', self.vmlinux.symbols.values())
			boundaries = set(map(attrgetter('vaddr'), boundaries))
			boundaries.discard(cur_idx_vaddr)
			vaddrs = []

			for vaddr in self.__iter_unpack_vmlinux_long(tbl_file_off):
				# Stop at the first vaddr pointing outside target_section
				if not (vstart <= vaddr < vend):
					break

				# Stop if we collide with another symbol right after the syscall
				# table (may be another syscall table e.g. the compat one)
				if cur_idx_vaddr in boundaries:
					break

				vaddrs.append(vaddr)
				cur_idx_vaddr += self.__long_size

			logging.info('Syscall table seems to be %d bytes, %d entries',
				cur_idx_vaddr - tbl.vaddr, len(vaddrs))

		return vaddrs

	def __extract_syscalls(self) -> List[Syscall]:
		if self.arch.bits32 != self.vmlinux.bits32:
			a, b = (32, 64) if self.arch.bits32 else (64, 32)
			logging.critical('Selected arch is %d-bit, but kernel is %d-bit', a, b)
			return []

		self.arch.adjust_abi(self.vmlinux)
		logging.debug('Arch: %r', self.arch)

		syscall_table_name = self.arch.syscall_table_name
		tbl = self.vmlinux.symbols.get(syscall_table_name)
		ni_syscalls = set()

		if not tbl:
			logging.critical('Unable to find %s symbol!', syscall_table_name)
			return []

		logging.debug('Syscall table: %r', tbl)

		# Read and parse the syscall table unpacking all virtual addresses it
		# contains

		text = self.vmlinux.sections['.text']

		if self.arch.uses_function_descriptors:
			opd = self.vmlinux.sections.get('.opd')
			if not opd:
				logging.critical('Arch uses function descriptors, but vmlinux '
					'has no .opd section!')
				return []

			descriptors = self.__unpack_syscall_table(tbl, opd)
			text_vstart = text.vaddr
			text_vend   = text_vstart + text.size
			vaddrs      = []

			# Translate function descriptors (one more level of indirection)
			for desc_vaddr in descriptors:
				vaddr = self.vmlinux.vaddr_read(desc_vaddr, self.__long_size)
				vaddr = struct.unpack(self.__long_pack_fmt, vaddr)[0]

				if not (text_vstart <= vaddr < text_vend):
					logging.warn('Function descriptor at 0x%x points outside '
						'.text: something is off!', desc_vaddr)

				vaddrs.append(vaddr)
		else:
			vaddrs = self.__unpack_syscall_table(tbl, text)

		if not vaddrs:
			logging.critical('Could not extract any valid function pointer '
				'from %s, giving up!', syscall_table_name)
			logging.critical('Is the kernel relocatable? Relocation entries '
				'for the syscall table are not supported.')
			return []

		# Find all ni_syscall symbols (there might be multiple) and keep track
		# of them for later in order to detect non-implemented syscalls.
		for sym in self.vmlinux.functions.values():
			if self.arch.symbol_is_ni_syscall(sym):
				ni_syscalls.add(sym)
				logging.debug('Found ni_syscall: %r', sym)

		if not ni_syscalls:
			logging.critical('No ni_syscall found!')
			return []

		seen = set(vaddrs)
		symbols_by_vaddr = {sym.vaddr: sym for sym in ni_syscalls}

		# Create a mapping vaddr -> symbol for every vaddr in the syscall table
		# for convenience.
		for sym in self.vmlinux.symbols.values():
			vaddr = sym.vaddr
			if vaddr not in seen:
				continue

			other = symbols_by_vaddr.get(vaddr)
			if sym == other:
				continue

			if other is not None:
				if other in ni_syscalls and sym not in ni_syscalls:
					# Don't allow other symbols to "override" a ni_syscall
					logging.debug('Discarding alias for %s: %s', other.name, sym.name)
					continue

				pref = self.arch.preferred_symbol(sym, other)
				sym, other = pref, (other if pref is sym else sym)

				if high_verbosity():
					logging.debug('Preferring %s over %s', pref.name, other.name)

			symbols_by_vaddr[vaddr] = sym

		del seen

		if not symbols_by_vaddr:
			logging.critical('Unable to find any symbol in the syscall table, giving up!')
			logging.critical('Is "%s" the correct arch/ABI combination for '
				'this kernel?', self.arch_name)
			return []

		# Sanity check: the only repeated vaddrs in the syscall table should be
		# the ones for *_ni_syscall. Warn in case there are others.
		counts = sorted(Counter(vaddrs).items(), key=itemgetter(1), reverse=True)
		best = symbols_by_vaddr[counts[0][0]]

		if best not in ni_syscalls:
			logging.error('Interesting! I was expecting *_ni_syscall to be the '
				'most frequent symbol in the syscall table, but %s is ('
				'appearing %d times).', best.name, counts[0][1])

		for va, n in counts[1:]:
			if n == 1:
				break

			logging.warn('Interesting! Vaddr 0x%x (%s) found %d times in %s',
				va, symbols_by_vaddr.get(va, '<unknown>'), n, syscall_table_name)

		symbols      = []
		symbol_names = []
		ni_count     = defaultdict(int)

		# Filter out only defined syscalls
		for idx, vaddr in enumerate(vaddrs):
			sym = symbols_by_vaddr.get(vaddr)
			if sym is None:
				logging.error('Unable to find symbol for %s[%d]: 0x%x',
					syscall_table_name, idx, vaddr)
				continue

			if high_verbosity():
				logging.debug('%s[%d]: %s', syscall_table_name, idx, sym)

			if sym in ni_syscalls:
				ni_count[sym.name] += 1
				continue

			symbols.append((idx, sym))
			symbol_names.append(sym.name)

		# Find common syscall symbol prefixes (e.g. "__x64_sys_") in order to be
		# able to strip them later to obtain the actual syscall name
		prefixes = common_syscall_symbol_prefixes(symbol_names, 20)
		logging.info('Common syscall symbol prefixes: %s', ', '.join(prefixes))

		syscalls  = []
		n_skipped = 0

		# Build list of syscalls (with prefixes stripped from the names) and
		# skip uneeded ones (e.g. implemented for other ABIs)
		for idx, sym in symbols:
			num      = self.arch.syscall_num_base + idx
			origname = noprefix(sym.name, *prefixes)
			origname = self.arch.translate_syscall_symbol_name(origname)
			name     = self.arch.normalize_syscall_name(origname)
			kdeps    = kconfig_syscall_deps(name, self.version)

			# We could need the original name to differentiate some syscalls
			# in order to understand if they need some Kconfig or not
			if not kdeps:
				kdeps = kconfig_syscall_deps(origname, self.version)

			sc = Syscall(idx, num, name, origname, sym, kdeps)

			if self.arch.skip_syscall(sc):
				logging.debug('Skipping %s', sym.name)
				n_skipped += 1
				continue

			syscalls.append(sc)

		# Add esoteric syscalls to the list, if any (these do not need any name
		# translation or signature search as they are hardcoded)
		esoteric = self.arch.esoteric_syscalls[self.version]
		n_esoteric = len(esoteric)

		for num, name, sym_name, sig in esoteric:
			sym = self.vmlinux.symbols[sym_name]
			syscalls.append(Syscall(None, num, name, name, sym, None, signature=sig, esoteric=True))

		ni_total = 0
		for name, n in sorted(ni_count.items(), key=itemgetter(1), reverse=True):
			logging.info('%d syscall table entries point to %s', n, name)
			ni_total += n

		assert len(syscalls) == len(vaddrs) - ni_total - n_skipped + n_esoteric

		# Find locations and signatures for all the syscalls we found (except
		# esoteric ones).
		extract_syscall_locations(syscalls, self.vmlinux, self.kdir, self.rdir, self.arch)
		extract_syscall_signatures(syscalls, self.vmlinux, self.kdir is not None)

		# Extract only implemented syscalls, warn for potentially bad matches
		# and filter out invalid ones.
		implemented  = []
		bad_loc_info = []
		no_loc_info  = []
		no_sig_info  = []

		for sc in syscalls:
			file, line, good = sc.file, sc.line, sc.good_location

			if not sc.esoteric:
				# Some syscalls are just a dummy function that does
				# `return -ENOSYS` or some other error, meaning that the syscall
				# is not actually implemented, even if present inthe syscall
				# table. We can filter those out on archs for which we have
				# .is_dummy_syscall() implemented, but we're not guaranteed to
				# catch everything. For example, .is_dummy_syscall() is useless
				# if the symbol has bad/zero size. Unless we check sources, we
				# can always have false positives even after this step.
				if self.arch.is_dummy_syscall(sc, self.vmlinux):
					continue

				if not good and file is not None:
					if file.match('**/kernel/sys_ni.c'):
						# If we got to this point the location is still not
						# "good" and points to sys_ni.c even after
						# adjusting/grepping. Assume the syscall is not
						# implemented. Granted, this could in theory lead to
						# false negatives, but I did not encounter one yet.
						# Since we are grepping the source code this should NOT
						# happen for implemented syscalls. Nonetheless warn
						# about it, so we can double check and make sure
						# everything is fine.
						logging.warn('Assuming %s is not implemented as it '
							'points to %s:%d after adjustments', sc.name,
							self.__rel(file), line)
						continue

					if self.kdir:
						if file.match('*.S'):
							hint = ' (implemented in asm?)'
						elif file.match('*.c'):
							hint = ' (normal function w/o asmlinkage?)'
						else:
							hint = ''

						bad_loc_info.append((
							sc.name,
							sc.symbol.name,
							self.__rel(file),
							str(line),
							hint
						))

			if file is None and self.can_extract_location_info:
				no_loc_info.append((sc.name, sc.symbol.name))

			if sc.signature is None and self.can_extract_signature_info:
				no_sig_info.append((sc.name, sc.symbol.name))

			implemented.append(sc)

		for info in bad_loc_info:
			logging.warn('Potentially bad location for %s (%s): %s:%s%s', *info)

		for info in no_loc_info:
			logging.error('Unable to find location for %s (%s)', *info)

		for info in no_sig_info:
			logging.error('Unable to extract signature for %s (%s)', *info)

		return implemented

	def __try_set_optimization_level(self, lvl: int) -> bool:
		# Might be the most ignorant thing in this whole codebase :')

		with (self.kdir / 'Makefile').open('r+') as f:
			self.__backup_makefile = data = f.read()
			assert f.seek(0) == 0

			match = re.search(r'^KBUILD_CFLAGS\s*\+=\s*-O(2)\n', data, re.MULTILINE)
			if not match:
				return False

			start, end = match.span(1)
			f.write(data[:start] + str(lvl) + data[end:])
			f.truncate()

		return True

	def __restore_makefile(self):
		if self.__backup_makefile:
			with (self.kdir / 'Makefile').open('w') as f:
				f.write(self.__backup_makefile)
		else:
			logging.error('Restoring Makefile without backing it up first???')

		atexit.unregister(self.__restore_makefile)

	def make(self, target: str, stdin=None, ensure=True) -> int:
		j = max(len(sched_getaffinity(0)) - 1, 1)
		cmd = ['make', f'-j{j}', f'ARCH={self.arch.name}']

		# Generate debug info with relative paths to make our life easier for
		# later analysis.
		cmd += [f"KCFLAGS='-fdebug-prefix-map={self.kdir.absolute()}=.'"]

		if self.toolchain_prefix:
			cmd += [f'CROSS_COMPILE={self.toolchain_prefix}']
		if self.outdir:
			cmd += [f'O={self.outdir}']

		if ensure:
			ensure_command(cmd + [target], self.kdir, stdin, False, high_verbosity())
			return 0

		return run_command(cmd + [target], self.kdir, stdin, high_verbosity())

	def sync_config(self):
		'''Set any config that was "unlocked" by others to its default value.
		The make target for this depends on the kernel version.
		'''
		if self.version >= (3, 7):
			self.make('olddefconfig')
		else:
			# Ugly, but oldconfig can error out if no input is given.
			self.make('oldconfig', stdin='\n' * 1000)

	def clean(self):
		self.__version = None
		self.make('distclean')

	def configure(self):
		config_file = (self.outdir or self.kdir) / '.config'
		self.__version = None

		logging.info('Configuring for Arch: %r', self.arch)
		logging.info('Base config target: %s', self.arch.config_target)
		self.make(self.arch.config_target)

		# TODO: maybe create a check that ensures these are actually applied and
		# consistent? E.G. check if all the configs that are supposed to exist
		# in a version actually exist when running the tool and keep the wanted
		# value after `make olddefconfig`.

		logging.info('Applying debugging configs')
		edit_config(self.kdir, config_file, kconfig_debugging(self.version))
		self.sync_config()

		logging.info('Applying compatibility configs')
		edit_config(self.kdir, config_file, kconfig_compatibility(self.version))
		self.sync_config()

		logging.info('Enabling more syscalls')
		edit_config_check_deps(self.kdir, config_file, kconfig_more_syscalls(self.version))
		self.sync_config()

		logging.info('Applying arch-specific configs')
		edit_config_check_deps(self.kdir, config_file, self.arch.kconfig[self.version])
		self.sync_config()

	def build(self, try_disable_opt: bool = False) -> float:
		start = monotonic()
		self.__version = None

		if try_disable_opt:
			logging.info('Trying to build with optimizations disabled (-O0)')

			# This will either work or fail for any level. If it fails, just
			# do a normal build with ensure=True, which will exit in case of
			# failure.
			if self.__try_set_optimization_level(0):
				atexit.register(self.__restore_makefile)
				res = self.make('vmlinux', ensure=False)
				self.__restore_makefile()

				if res == 0:
					return monotonic() - start

				logging.error('Failed to build with -O0, trying -O1')

				self.__try_set_optimization_level(1)
				res = self.make('vmlinux', ensure=False)
				self.__restore_makefile()

				if res == 0:
					return monotonic() - start

				logging.error('Failed to build with -O1, doing a normal build')
			else:
				logging.warn('Unable to patch Makefile to disable '
					'optimizations, doing a normal build instead')

		self.make('vmlinux')
		return monotonic() - start
