// Standalone build of the scanner, for testing without the game:
//   g++ -O2 -I src/addonchecker tools/addon_checker_cli.cpp src/addonchecker/addon_scanner.cpp -o addon_checker
//   ./addon_checker "<Steam>/steamapps/common/Half-Life 2/hl2/custom" [more custom folders...]
#include <stdio.h>
#include <stdlib.h>
#include "addon_scanner.h"

int main( int argc, char **argv )
{
	if ( argc < 2 )
	{
		fprintf( stderr, "usage: %s <custom folder> [more folders...]\n", argv[0] );
		return 1;
	}

	std::vector<std::string> dirs( argv + 1, argv + argc );
	AddonChecker::Report report = AddonChecker::Scan( dirs, std::vector<std::string>(), 1024 );
	fputs( AddonChecker::FormatReport( report, 0 ).c_str(), stdout );
	return report.conflicts.empty() ? 0 : 2;
}
