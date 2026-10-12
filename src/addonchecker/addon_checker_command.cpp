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
//
// The scan runs on a worker thread so the game doesn't freeze while it reads
// big loose-file addons; the report is printed once it finishes.
//========================================================================
#include "cbase.h"
#include "filesystem.h"
#include "tier0/icommandline.h"
#include "igamesystem.h"
#include "tier0/threadtools.h"

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

	// An addon's search path is <somewhere>/custom/<addon>/ or <somewhere>/custom/<addon>.vpk,
	// so look at its parent folder rather than for "/custom/" anywhere in the path.
	for ( size_t i = 0; i < loadOrder.size(); i++ )
	{
		std::string path = loadOrder[i];
		while ( !path.empty() && ( path[path.size() - 1] == '/' || path[path.size() - 1] == '\\' ) )
			path.erase( path.size() - 1 );
		size_t slash = path.find_last_of( "/\\" );
		if ( slash == std::string::npos )
			continue;
		std::string parent = path.substr( 0, slash ); // keep original case for the OS
		std::string norm = AddonChecker::NormalizePath( parent );
		if ( norm.size() >= 7 && norm.compare( norm.size() - 7, 7, "/custom" ) == 0 )
			dirs.push_back( parent );
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

class CAddonScanThread : public CThread
{
public:
	CAddonScanThread() : m_MinDupeSize( 1 ) {}

	virtual int Run()
	{
		m_Report = AddonChecker::Scan( m_Dirs, m_LoadOrder, m_MinDupeSize );
		m_Done = 1;
		return 0;
	}

	// Set on the main thread before Start(), read by the worker.
	std::vector<std::string> m_Dirs;
	std::vector<std::string> m_LoadOrder;
	uint64_t                 m_MinDupeSize;

	// Set by the worker; read on the main thread once m_Done is set.
	AddonChecker::Report     m_Report;
	CInterlockedInt          m_Done;
};

static CAddonScanThread *s_pScan = NULL;

// Main thread: write the report file and print the summary.
static void FinishAddonCheck( const AddonChecker::Report &report )
{
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

static void StartAddonCheck( const std::vector<std::string> &explicitDirs )
{
	if ( s_pScan )
	{
		Msg( "[Addon Checker] A scan is already running.\n" );
		return;
	}

	// Everything that touches the engine happens here, on the main thread.
	CAddonScanThread *scan = new CAddonScanThread;
	GetLoadOrder( scan->m_LoadOrder );
	scan->m_Dirs = explicitDirs;
	if ( scan->m_Dirs.empty() )
		GetCustomDirs( scan->m_LoadOrder, scan->m_Dirs );
	scan->m_MinDupeSize = (uint64_t)MAX( addon_check_dupe_minsize.GetInt(), 1 );

	for ( size_t i = 0; i < scan->m_Dirs.size(); i++ )
		Msg( "[Addon Checker] Scanning %s\n", scan->m_Dirs[i].c_str() );

	scan->SetName( "AddonChecker" );
	if ( scan->Start() )
	{
		s_pScan = scan; // CAddonCheckSystem::Update() picks up the result
		return;
	}

	// Couldn't start a thread: scan here instead.
	scan->Run();
	FinishAddonCheck( scan->m_Report );
	delete scan;
}

CON_COMMAND( addon_check, "Check custom/ addons for conflicting and duplicate files. Optional: folders to scan instead." )
{
	std::vector<std::string> dirs;
	for ( int i = 1; i < args.ArgC(); i++ )
		dirs.push_back( args[i] );
	StartAddonCheck( dirs );
}

class CAddonCheckSystem : public CAutoGameSystemPerFrame
{
public:
	CAddonCheckSystem() : CAutoGameSystemPerFrame( "CAddonCheckSystem" ) {}

	// -addoncheck on the command line runs the scan once at startup.
	virtual void PostInit()
	{
		if ( CommandLine()->CheckParm( "-addoncheck" ) )
			StartAddonCheck( std::vector<std::string>() );
	}

	// Runs every frame, in the menus too.
	virtual void Update( float /*frametime*/ )
	{
		if ( !s_pScan || !s_pScan->m_Done )
			return;
		s_pScan->Join();
		FinishAddonCheck( s_pScan->m_Report );
		delete s_pScan;
		s_pScan = NULL;
	}

	// Quitting mid-scan: wait for the worker, the DLL is about to unload.
	virtual void Shutdown()
	{
		if ( !s_pScan )
			return;
		s_pScan->Join();
		delete s_pScan;
		s_pScan = NULL;
	}
};

static CAddonCheckSystem s_AddonCheckSystem;
