// Standalone build of the scanner, for testing without the game:
//   g++ -O2 -I src/addonchecker tools/addon_checker_cli.cpp src/addonchecker/addon_scanner.cpp -o addon_checker
//   ./addon_checker "<Steam>/steamapps/common/Half-Life 2/hl2/custom" [more custom folders...]
//
// --order "<path>;<path>;..." gives the load order, highest priority first,
// in the same form as the engine's search path (path_info / GetSearchPath).
// Exit code: 0 = clean, 2 = conflicts or mixed models found, 1 = bad usage.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "addon_scanner.h"

static void Usage( const char *exe )
{
	fprintf( stderr, "usage: %s [--order \"<path>;<path>;...\"] <custom folder> [more folders...]\n", exe );
}

int main( int argc, char **argv )
{
	std::vector<std::string> dirs, loadOrder;
	for ( int i = 1; i < argc; i++ )
	{
		if ( !strcmp( argv[i], "--order" ) )
		{
			if ( ++i >= argc )
			{
				Usage( argv[0] );
				return 1;
			}
			std::string all = argv[i];
			size_t start = 0;
			while ( start < all.size() )
			{
				size_t end = all.find( ';', start );
				if ( end == std::string::npos )
					end = all.size();
				if ( end > start )
					loadOrder.push_back( all.substr( start, end - start ) );
				start = end + 1;
			}
		}
		else
			dirs.push_back( argv[i] );
	}

	if ( dirs.empty() )
	{
		Usage( argv[0] );
		return 1;
	}

	AddonChecker::Report report = AddonChecker::Scan( dirs, loadOrder, 1024 );
	fputs( AddonChecker::FormatReport( report, 0 ).c_str(), stdout );
	return report.conflicts.empty() && report.mixedModels.empty() ? 0 : 2;
}
