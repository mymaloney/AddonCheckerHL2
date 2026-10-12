//========================================================================
// Addon Checker - Source SDK 2013 client DLL hookup.
//
// Console:
//   addon_check                 scan custom/ folders, print + write addon_report.txt
//   addon_check <folder> ...    scan specific custom folders instead
//   addon_check_console_limit   entries per section printed to console (0 = all)
//   addon_check_dupe_minsize    smallest file checked for content duplicates
// Launch option:
//   -addoncheck                 run the scan once when the game starts
//========================================================================
#include "cbase.h"
#include "filesystem.h"
#include "tier0/icommandline.h"
#include "igamesystem.h"

#include "addon_scanner.h"

// memdbgon must be the last include file in a .cpp file!!!
#include "tier0/memdbgon.h"

static ConVar addon_check_console_limit( "addon_check_console_limit", "25", FCVAR_ARCHIVE,
	"Entries per section that addon_check prints to the console (0 = all). The report file always has everything." );
static ConVar addon_check_dupe_minsize( "addon_check_dupe_minsize", "1024", FCVAR_ARCHIVE,
	"Smallest file size in bytes that addon_check compares for duplicate content under different names." );

static const char *REPORT_FILE = "addon_report.txt";

// Every GAME search path, highest priority first. Tells us which copy wins.
static void GetLoadOrder( std::vector<std::string> &order )
{
	// Returns the length it needs; grow until it fits.
	std::vector<char> buf( 16 * 1024, 0 );
	for ( ;; )
	{
		int needed = g_pFullFileSystem->GetSearchPath( "GAME", true, &buf[0], (int)buf.size() );
		if ( needed < (int)buf.size() || buf.size() >= 4 * 1024 * 1024 )
			break;
		buf.assign( needed + 1, 0 );
	}
	buf[buf.size() - 1] = 0;

	std::string all( &buf[0] );
	size_t start = 0;
	while ( start < all.size() )
	{
		size_t end = all.find( ';', start );
		if ( end == std::string::npos )
			end = all.size();
		if ( end > start )
			order.push_back( all.substr( start, end - start ) );
		start = end + 1;
	}
}

// The mod's custom/ folder, plus every other custom/ folder the engine mounted
// (e.g. hl2/custom when the mod mounts Half-Life 2's content).
static void GetCustomDirs( const std::vector<std::string> &loadOrder, std::vector<std::string> &dirs )
{
	dirs.push_back( std::string( engine->GetGameDirectory() ) + "/custom" );

	for ( size_t i = 0; i < loadOrder.size(); i++ )
	{
		std::string norm = AddonChecker::NormalizePath( loadOrder[i] );
		size_t pos = norm.find( "/custom/" );
		if ( pos != std::string::npos )
			dirs.push_back( loadOrder[i].substr( 0, pos + 7 ) ); // keep original case for the OS
	}
}

static void PrintReport( const std::string &text )
{
	static const Color header( 255, 200, 80, 255 );
	static const Color warn( 255, 110, 90, 255 );

	size_t start = 0;
	while ( start < text.size() )
	{
		size_t end = text.find( '\n', start );
		if ( end == std::string::npos )
			end = text.size();
		std::string line = text.substr( start, end - start ) + "\n";

		if ( line.compare( 0, 3, "== " ) == 0 )
			ConColorMsg( header, "%s", line.c_str() );
		else if ( line.find( "[!]" ) != std::string::npos )
			ConColorMsg( warn, "%s", line.c_str() );
		else
			Msg( "%s", line.c_str() );

		start = end + 1;
	}
}

static void RunAddonCheck( const std::vector<std::string> &explicitDirs )
{
	std::vector<std::string> loadOrder;
	GetLoadOrder( loadOrder );

	std::vector<std::string> dirs = explicitDirs;
	if ( dirs.empty() )
		GetCustomDirs( loadOrder, dirs );

	for ( size_t i = 0; i < dirs.size(); i++ )
		Msg( "[Addon Checker] Scanning %s\n", dirs[i].c_str() );

	AddonChecker::Report report = AddonChecker::Scan( dirs, loadOrder,
		(uint64_t)MAX( addon_check_dupe_minsize.GetInt(), 1 ) );

	FileHandle_t fh = g_pFullFileSystem->Open( REPORT_FILE, "wb", "MOD" );
	if ( fh )
	{
		std::string full = AddonChecker::FormatReport( report, 0 );
		g_pFullFileSystem->Write( full.c_str(), (int)full.size(), fh );
		g_pFullFileSystem->Close( fh );
	}

	PrintReport( AddonChecker::FormatReport( report, (size_t)MAX( addon_check_console_limit.GetInt(), 0 ) ) );

	if ( fh )
		ConColorMsg( Color( 120, 220, 120, 255 ), "[Addon Checker] Full report: %s/%s\n", engine->GetGameDirectory(), REPORT_FILE );
	else
		Warning( "[Addon Checker] Could not write %s\n", REPORT_FILE );
}

CON_COMMAND( addon_check, "Check custom/ addons for conflicting and duplicate files. Optional: folders to scan instead." )
{
	std::vector<std::string> dirs;
	for ( int i = 1; i < args.ArgC(); i++ )
		dirs.push_back( args[i] );
	RunAddonCheck( dirs );
}

// -addoncheck on the command line runs the scan once at startup.
class CAddonCheckStartup : public CAutoGameSystem
{
public:
	CAddonCheckStartup() : CAutoGameSystem( "CAddonCheckStartup" ) {}

	virtual void PostInit()
	{
		if ( CommandLine()->CheckParm( "-addoncheck" ) )
			RunAddonCheck( std::vector<std::string>() );
	}
};

static CAddonCheckStartup s_AddonCheckStartup;
