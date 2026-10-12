#!/bin/sh
# Compiles the game-side files against Source SDK 2013 headers (syntax only),
# with the defines the Linux client project uses. Catches SDK API mistakes
# without building the whole client.
#
#   tests/check_sdk_build.sh <path to source-sdk-2013 checkout>
#
# Only these SDK folders are needed: src/common src/public src/game/client src/game/shared
set -e
SDK=${1:?usage: $0 <source-sdk-2013 checkout>}/src
HERE=$(cd "$(dirname "$0")/.." && pwd)
CXX=${CXX:-g++}

LOG=${TMPDIR:-/tmp}/sdk_check.log
for f in addon_checker_command.cpp addon_scanner.cpp; do
	echo "checking $f"
	status=0
	$CXX -std=gnu++17 -fsyntax-only -m64 -Wall -Wextra -Wno-unknown-pragmas \
		-DGNUC -DPOSIX -D_POSIX -DCOMPILER_GCC -D_LINUX -DLINUX -DPLATFORM_64BITS -DNDEBUG \
		-DVPC -DSOURCESDK -D_DLL_EXT=.so \
		-DNO_STRING_T -DCLIENT_DLL -DVECTOR -DVERSION_SAFE_STEAM_API_INTERFACES -DPROTECTED_THINGS_ENABLE \
		-Dstrncpy=use_Q_strncpy_instead -D_snprintf=use_Q_snprintf_instead \
		-DINCLUDED_STEAM2_USERID_STRUCTS -DHL2_CLIENT_DLL \
		-I"$SDK/common" -I"$SDK/public" -I"$SDK/public/tier0" -I"$SDK/public/tier1" \
		-I"$SDK/game/client" -I"$SDK/game/shared" -I"$SDK/game/client/hl2" -I"$SDK/game/shared/hl2" \
		-I"$HERE/src/addonchecker" \
		"$HERE/src/addonchecker/$f" >"$LOG" 2>&1 || status=$?
	cat "$LOG"
	if [ $status -ne 0 ]; then
		echo "$f doesn't compile against the SDK"
		exit 1
	fi
	# Warnings from the SDK's own headers are not ours to fix; ours fail the check.
	if grep -q "src/addonchecker/.*\(warning\|error\)" "$LOG"; then
		echo "warnings or errors in $f"
		exit 1
	fi
done
echo "ok"
