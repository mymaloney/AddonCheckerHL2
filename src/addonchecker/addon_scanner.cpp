//========================================================================
// Addon Checker - scanner. See addon_scanner.h.
//========================================================================
// The client project defines _snprintf as use_Q_snprintf_instead to steer SDK
// code to Q_snprintf. This file doesn't use the SDK and needs the real one on
// VS2013 and older, so undo that before any CRT header declares it.
#undef _snprintf

#include "addon_scanner.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <algorithm>
#include <map>
#include <set>
#include <utility>

#if defined( _MSC_VER ) && _MSC_VER < 1900
#define snprintf _snprintf // VS2013 and older
#endif

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <dirent.h>
#include <sys/stat.h>
#endif

namespace AddonChecker
{

//------------------------------------------------------------------------
// Helpers
//------------------------------------------------------------------------

std::string NormalizePath( const std::string &path )
{
	std::string out = path;
	for ( size_t i = 0; i < out.size(); i++ )
	{
		char c = out[i];
		if ( c == '\\' )
			c = '/';
		else if ( c >= 'A' && c <= 'Z' )
			c = (char)( c - 'A' + 'a' );
		out[i] = c;
	}
	while ( out.size() > 1 && out[out.size() - 1] == '/' )
		out.erase( out.size() - 1 );
	return out;
}

static bool StartsWith( const std::string &s, const char *prefix )
{
	return s.compare( 0, strlen( prefix ), prefix ) == 0;
}

static bool EndsWith( const std::string &s, const char *suffix )
{
	size_t n = strlen( suffix );
	return s.size() >= n && s.compare( s.size() - n, n, suffix ) == 0;
}

static std::string JoinPath( const std::string &a, const std::string &b )
{
	if ( a.empty() ) return b;
	if ( b.empty() ) return a;
	return a + "/" + b;
}

struct DirEntry
{
	std::string name;
	bool        isDir;
};

struct ByName
{
	bool operator()( const DirEntry &a, const DirEntry &b ) const
	{
		std::string la = NormalizePath( a.name ), lb = NormalizePath( b.name );
		if ( la != lb ) return la < lb;
		return a.name < b.name;
	}
};

static bool ListDir( const std::string &dir, std::vector<DirEntry> &out )
{
#ifdef _WIN32
	WIN32_FIND_DATAA fd;
	HANDLE h = FindFirstFileA( ( dir + "\\*" ).c_str(), &fd );
	if ( h == INVALID_HANDLE_VALUE )
		return false;
	do
	{
		if ( !strcmp( fd.cFileName, "." ) || !strcmp( fd.cFileName, ".." ) )
			continue;
		DirEntry e;
		e.name = fd.cFileName;
		e.isDir = ( fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY ) != 0;
		out.push_back( e );
	} while ( FindNextFileA( h, &fd ) );
	FindClose( h );
#else
	DIR *d = opendir( dir.c_str() );
	if ( !d )
		return false;
	while ( struct dirent *ent = readdir( d ) )
	{
		if ( !strcmp( ent->d_name, "." ) || !strcmp( ent->d_name, ".." ) )
			continue;
		struct stat st;
		if ( stat( ( dir + "/" + ent->d_name ).c_str(), &st ) != 0 )
			continue;
		DirEntry e;
		e.name = ent->d_name;
		e.isDir = S_ISDIR( st.st_mode );
		out.push_back( e );
	}
	closedir( d );
#endif
	// The OS lists in no particular order; sort so every scan gives the same report.
	std::sort( out.begin(), out.end(), ByName() );
	return true;
}

// Identifies a folder on disk (following symlinks/junctions), so a link back
// to a parent folder can't make the walk recurse forever.
typedef std::pair<uint64_t, uint64_t> DirId;

static bool GetDirId( const std::string &dir, DirId &id )
{
#ifdef _WIN32
	HANDLE h = CreateFileA( dir.c_str(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
		NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL );
	if ( h == INVALID_HANDLE_VALUE )
		return false;
	BY_HANDLE_FILE_INFORMATION info;
	BOOL ok = GetFileInformationByHandle( h, &info );
	CloseHandle( h );
	if ( !ok )
		return false;
	id.first = info.dwVolumeSerialNumber;
	id.second = ( (uint64_t)info.nFileIndexHigh << 32 ) | info.nFileIndexLow;
#else
	struct stat st;
	if ( stat( dir.c_str(), &st ) != 0 )
		return false;
	id.first = (uint64_t)st.st_dev;
	id.second = (uint64_t)st.st_ino;
#endif
	return true;
}

//------------------------------------------------------------------------
// CRC32 (IEEE, the same one VPK directories store per file)
//------------------------------------------------------------------------

static uint32_t s_CrcTable[256];

static void InitCrcTable()
{
	static bool done = false;
	if ( done )
		return;
	for ( uint32_t i = 0; i < 256; i++ )
	{
		uint32_t c = i;
		for ( int k = 0; k < 8; k++ )
			c = ( c & 1 ) ? 0xEDB88320u ^ ( c >> 1 ) : c >> 1;
		s_CrcTable[i] = c;
	}
	done = true;
}

static bool CrcFile( const std::string &path, uint32_t &crc, uint64_t &size )
{
	FILE *f = fopen( path.c_str(), "rb" );
	if ( !f )
		return false;

	InitCrcTable();
	uint32_t c = 0xFFFFFFFFu;
	size = 0;
	unsigned char buf[64 * 1024];
	size_t n;
	while ( ( n = fread( buf, 1, sizeof( buf ), f ) ) > 0 )
	{
		for ( size_t i = 0; i < n; i++ )
			c = s_CrcTable[( c ^ buf[i] ) & 0xFF] ^ ( c >> 8 );
		size += n;
	}
	bool ok = !ferror( f );
	fclose( f );
	crc = c ^ 0xFFFFFFFFu;
	return ok;
}

//------------------------------------------------------------------------
// VPK directory reader (versions 1 and 2)
//------------------------------------------------------------------------

static uint32_t ReadU32( const unsigned char *p ) { return p[0] | ( p[1] << 8 ) | ( p[2] << 16 ) | ( (uint32_t)p[3] << 24 ); }
static uint16_t ReadU16( const unsigned char *p ) { return (uint16_t)( p[0] | ( p[1] << 8 ) ); }

class TreeCursor
{
public:
	TreeCursor( const std::vector<unsigned char> &data ) : m_Data( data ), m_Pos( 0 ), m_Ok( true ) {}

	std::string String()
	{
		size_t start = m_Pos;
		while ( m_Pos < m_Data.size() && m_Data[m_Pos] != 0 )
			m_Pos++;
		if ( m_Pos >= m_Data.size() )
		{
			m_Ok = false;
			return std::string();
		}
		std::string s( (const char *)&m_Data[start], m_Pos - start );
		m_Pos++; // null terminator
		return s;
	}

	const unsigned char *Bytes( size_t n )
	{
		if ( m_Data.size() - m_Pos < n )
		{
			m_Ok = false;
			return NULL;
		}
		const unsigned char *p = &m_Data[m_Pos];
		m_Pos += n;
		return p;
	}

	bool Ok() const { return m_Ok; }

private:
	const std::vector<unsigned char> &m_Data;
	size_t m_Pos;
	bool   m_Ok;
};

// Appends the VPK's files to addon.files, returns false and sets addon.error on failure.
static bool ReadVpk( const std::string &vpkPath, Addon &addon )
{
	FILE *f = fopen( vpkPath.c_str(), "rb" );
	if ( !f )
	{
		addon.error = "could not open " + vpkPath;
		return false;
	}

	unsigned char header[12];
	if ( fread( header, 1, sizeof( header ), f ) != sizeof( header ) || ReadU32( header ) != 0x55AA1234u )
	{
		fclose( f );
		addon.error = "not a VPK directory file: " + vpkPath;
		return false;
	}

	uint32_t version = ReadU32( header + 4 );
	uint32_t treeSize = ReadU32( header + 8 );
	if ( version == 2 )
		fseek( f, 16, SEEK_CUR ); // section sizes we don't need
	else if ( version != 1 )
	{
		fclose( f );
		addon.error = "unsupported VPK version in " + vpkPath;
		return false;
	}

	// Read in chunks rather than allocating treeSize up front: a corrupt header
	// could otherwise ask for gigabytes before we find out the file is short.
	std::vector<unsigned char> tree;
	bool readOk = true;
	while ( tree.size() < treeSize )
	{
		size_t have = tree.size();
		size_t chunk = std::min<size_t>( treeSize - have, 1024 * 1024 );
		tree.resize( have + chunk );
		if ( fread( &tree[have], 1, chunk, f ) != chunk )
		{
			readOk = false;
			break;
		}
	}
	fclose( f );
	if ( !readOk )
	{
		addon.error = "truncated VPK: " + vpkPath;
		return false;
	}

	// extension -> directory -> file name -> entry, each list ended by an empty string.
	// A single space means "none" (no extension / root directory).
	TreeCursor cur( tree );
	for ( ;; )
	{
		std::string ext = cur.String();
		if ( !cur.Ok() || ext.empty() ) break;
		for ( ;; )
		{
			std::string dir = cur.String();
			if ( !cur.Ok() || dir.empty() ) break;
			for ( ;; )
			{
				std::string name = cur.String();
				if ( !cur.Ok() || name.empty() ) break;

				const unsigned char *e = cur.Bytes( 18 );
				if ( !e ) break;
				uint32_t crc = ReadU32( e );
				uint16_t preload = ReadU16( e + 4 );
				uint32_t length = ReadU32( e + 12 );
				cur.Bytes( preload );
				if ( !cur.Ok() ) break;

				std::string path = ( dir == " " ) ? name : dir + "/" + name;
				if ( ext != " " )
					path += "." + ext;

				FileEntry fe;
				fe.path = NormalizePath( path );
				fe.crc = crc;
				fe.size = (uint64_t)preload + length;
				fe.readable = true;
				addon.files.push_back( fe );
			}
			if ( !cur.Ok() ) break;
		}
		if ( !cur.Ok() ) break;
	}

	if ( !cur.Ok() )
	{
		addon.error = "corrupt VPK directory (partially read): " + vpkPath;
		return false;
	}
	return true;
}

//------------------------------------------------------------------------
// Loose folder addons
//------------------------------------------------------------------------

// open = the folders we're currently inside, to stop at symlink loops.
static void WalkFolder( const std::string &root, const std::string &rel, Addon &addon, std::set<DirId> &open )
{
	std::string dir = rel.empty() ? root : root + "/" + rel;
	DirId id;
	bool haveId = GetDirId( dir, id );
	if ( haveId && !open.insert( id ).second )
		return; // links back to a folder we're already walking

	std::vector<DirEntry> entries;
	ListDir( dir, entries );

	for ( size_t i = 0; i < entries.size(); i++ )
	{
		const DirEntry &e = entries[i];
		std::string relPath = JoinPath( rel, e.name );

		if ( e.isDir )
		{
			WalkFolder( root, relPath, addon, open );
			continue;
		}

		if ( rel.empty() )
		{
			// The engine also mounts a pak01_dir.vpk inside a search path folder.
			if ( NormalizePath( e.name ) == "pak01_dir.vpk" )
			{
				Addon packed;
				if ( ReadVpk( root + "/" + e.name, packed ) || !packed.files.empty() )
					addon.files.insert( addon.files.end(), packed.files.begin(), packed.files.end() );
				if ( !packed.error.empty() )
					addon.error = packed.error;
			}
			// Other root files (addoninfo.txt, readme...) aren't game content.
			continue;
		}

		FileEntry fe;
		fe.path = NormalizePath( relPath );
		fe.crc = 0;
		fe.size = 0;
		fe.readable = CrcFile( root + "/" + relPath, fe.crc, fe.size );
		addon.files.push_back( fe );
	}

	if ( haveId )
		open.erase( id );
}

// foo_000.vpk, foo_001.vpk... hold the data for foo_dir.vpk, not separate addons.
static bool IsVpkArchivePart( const std::string &name )
{
	if ( name.size() < 9 || !EndsWith( name, ".vpk" ) )
		return false;
	size_t n = name.size();
	return name[n - 8] == '_' && isdigit( (unsigned char)name[n - 7] ) &&
	       isdigit( (unsigned char)name[n - 6] ) && isdigit( (unsigned char)name[n - 5] );
}

//------------------------------------------------------------------------
// Scan
//------------------------------------------------------------------------

static int FindLoadOrder( const Addon &addon, const std::vector<std::string> &order )
{
	std::string loc = NormalizePath( addon.location );
	std::string alt = loc;
	if ( EndsWith( alt, "_dir.vpk" ) )
		alt = alt.substr( 0, alt.size() - 8 ) + ".vpk";

	for ( size_t i = 0; i < order.size(); i++ )
	{
		std::string o = NormalizePath( order[i] );
		if ( o == loc || o == alt )
			return (int)i;
	}
	return -1;
}

static const FileEntry &FileOf( const Report &r, const Copy &c )
{
	return r.addons[c.addon].files[c.file];
}

static bool SameContents( const FileEntry &a, const FileEntry &b )
{
	return a.readable && b.readable && a.size == b.size && a.crc == b.crc;
}

static size_t DistinctAddons( const std::vector<Copy> &copies )
{
	std::set<size_t> s;
	for ( size_t i = 0; i < copies.size(); i++ )
		s.insert( copies[i].addon );
	return s.size();
}

// Higher = more likely to break the game when the wrong copy wins.
static int Severity( const std::string &path )
{
	if ( StartsWith( path, "scripts/" ) || StartsWith( path, "resource/" ) ||
	     StartsWith( path, "cfg/" ) || StartsWith( path, "maps/" ) )
		return 2;
	if ( StartsWith( path, "models/" ) )
		return 1;
	return 0;
}

struct ByAddonLoadOrder
{
	explicit ByAddonLoadOrder( const Report &r ) : m_Report( &r ) {}
	bool operator()( size_t a, size_t b ) const
	{
		return m_Report->addons[a].loadOrder < m_Report->addons[b].loadOrder;
	}
	const Report *m_Report;
};

struct ByLoadOrder
{
	explicit ByLoadOrder( const Report &r ) : m_Report( &r ) {}
	bool operator()( const Copy &a, const Copy &b ) const
	{
		return m_Report->addons[a.addon].loadOrder < m_Report->addons[b.addon].loadOrder;
	}
	const Report *m_Report;
};

struct BySeverity
{
	bool operator()( const PathGroup &a, const PathGroup &b ) const
	{
		int sa = Severity( a.path ), sb = Severity( b.path );
		if ( sa != sb ) return sa > sb;
		return a.path < b.path;
	}
};

struct BySize
{
	bool operator()( const ContentGroup &a, const ContentGroup &b ) const { return a.size > b.size; }
};

struct ByPath
{
	explicit ByPath( const Report &r ) : m_Report( &r ) {}
	bool operator()( const Copy &a, const Copy &b ) const
	{
		const std::string &pa = FileOf( *m_Report, a ).path, &pb = FileOf( *m_Report, b ).path;
		if ( pa != pb ) return pa < pb;
		return a.addon < b.addon;
	}
	const Report *m_Report;
};

static size_t DistinctPaths( const Report &r, const std::vector<Copy> &copies )
{
	std::set<std::string> s;
	for ( size_t i = 0; i < copies.size(); i++ )
		s.insert( FileOf( r, copies[i] ).path );
	return s.size();
}

//------------------------------------------------------------------------
// Mixed models
//------------------------------------------------------------------------

// The parts the engine loads for a model. The .dx80.vtx/.sw.vtx files are
// only used below DirectX 9, so a mismatch there doesn't show in game.
static const char *const s_ModelParts[] = { ".mdl", ".vvd", ".dx90.vtx", ".phy", ".ani" };
static const size_t NUM_MODEL_PARTS = sizeof( s_ModelParts ) / sizeof( s_ModelParts[0] );

// Index into s_ModelParts, or -1 if the path isn't part of a model.
static int ModelPartIndex( const std::string &path )
{
	if ( !StartsWith( path, "models/" ) )
		return -1;
	for ( size_t i = 0; i < NUM_MODEL_PARTS; i++ )
		if ( EndsWith( path, s_ModelParts[i] ) )
			return (int)i;
	return -1;
}

// The copy of a part that the addon ships, or NULL.
static const Copy *CopyFrom( const std::vector<Copy> &copies, size_t addon )
{
	for ( size_t i = 0; i < copies.size(); i++ )
		if ( copies[i].addon == addon )
			return &copies[i];
	return NULL;
}

// order = addons, highest priority first. The game takes each part from the
// first addon that has it. That's only safe if a single addon ships every one
// of those parts with the same contents, i.e. the model is all that addon's.
static bool IsMixed( const Report &r, const std::vector<std::vector<Copy> > &parts, const std::vector<size_t> &order )
{
	std::vector<const Copy *> used( parts.size(), (const Copy *)NULL );
	for ( size_t p = 0; p < parts.size(); p++ )
		for ( size_t o = 0; o < order.size() && !used[p]; o++ )
			used[p] = CopyFrom( parts[p], order[o] );

	for ( size_t o = 0; o < order.size(); o++ )
	{
		bool whole = true;
		for ( size_t p = 0; p < parts.size() && whole; p++ )
		{
			if ( !used[p] )
				continue;
			const Copy *own = CopyFrom( parts[p], order[o] );
			whole = own && ( own == used[p] || SameContents( FileOf( r, *own ), FileOf( r, *used[p] ) ) );
		}
		if ( whole )
			return false;
	}
	return true;
}

static void FindMixedModels( const std::map<std::string, std::vector<Copy> > &byPath, Report &r )
{
	// model path without extension -> copies of each part, indexed like s_ModelParts
	std::map<std::string, std::vector<std::vector<Copy> > > byModel;
	for ( std::map<std::string, std::vector<Copy> >::const_iterator it = byPath.begin(); it != byPath.end(); ++it )
	{
		int part = ModelPartIndex( it->first );
		if ( part < 0 )
			continue;
		std::vector<std::vector<Copy> > &parts = byModel[it->first.substr( 0, it->first.size() - strlen( s_ModelParts[part] ) )];
		parts.resize( NUM_MODEL_PARTS );
		parts[part] = it->second;
	}

	for ( std::map<std::string, std::vector<std::vector<Copy> > >::iterator it = byModel.begin(); it != byModel.end(); ++it )
	{
		const std::vector<std::vector<Copy> > &parts = it->second;

		std::vector<size_t> addons; // every addon with a part of this model
		for ( size_t p = 0; p < parts.size(); p++ )
			for ( size_t c = 0; c < parts[p].size(); c++ )
				addons.push_back( parts[p][c].addon );
		std::sort( addons.begin(), addons.end() );
		addons.erase( std::unique( addons.begin(), addons.end() ), addons.end() );
		if ( addons.size() < 2 )
			continue;

		bool orderKnown = true;
		for ( size_t a = 0; a < addons.size(); a++ )
			if ( r.addons[addons[a]].loadOrder < 0 )
				orderKnown = false;

		bool mixed;
		if ( orderKnown )
		{
			std::sort( addons.begin(), addons.end(), ByAddonLoadOrder( r ) );
			mixed = IsMixed( r, parts, addons );
		}
		else if ( addons.size() <= 6 )
		{
			// Order unknown: report it if any load order mixes the parts.
			// addons is sorted, which next_permutation needs to try them all.
			do
				mixed = IsMixed( r, parts, addons );
			while ( !mixed && std::next_permutation( addons.begin(), addons.end() ) );
		}
		else
		{
			mixed = true; // too many addons to try every order; flag it to be safe
		}

		if ( !mixed )
			continue;

		ModelGroup g;
		g.path = it->first;
		g.orderKnown = orderKnown;
		for ( size_t p = 0; p < parts.size(); p++ )
		{
			if ( parts[p].empty() )
				continue;
			ModelPart mp;
			mp.ext = s_ModelParts[p];
			mp.copies = parts[p];
			if ( orderKnown )
				std::stable_sort( mp.copies.begin(), mp.copies.end(), ByLoadOrder( r ) );
			g.parts.push_back( mp );
		}
		r.mixedModels.push_back( g );
	}
}

Report Scan( const std::vector<std::string> &customDirs,
             const std::vector<std::string> &loadOrder,
             uint64_t minDupeSize )
{
	Report r;
	r.totalFiles = 0;

	std::set<std::string> seenDirs;
	for ( size_t d = 0; d < customDirs.size(); d++ )
	{
		std::string dir = customDirs[d];
		while ( dir.size() > 1 && ( dir[dir.size() - 1] == '/' || dir[dir.size() - 1] == '\\' ) )
			dir.erase( dir.size() - 1 );
		if ( !seenDirs.insert( NormalizePath( dir ) ).second )
			continue;

		std::vector<DirEntry> entries;
		if ( !ListDir( dir, entries ) )
			continue;

		for ( size_t i = 0; i < entries.size(); i++ )
		{
			const DirEntry &e = entries[i];
			std::string lower = NormalizePath( e.name );

			Addon addon;
			addon.name = e.name;
			addon.location = dir + "/" + e.name;
			addon.isVpk = !e.isDir;
			addon.loadOrder = -1;

			if ( e.isDir )
			{
				std::set<DirId> open;
				WalkFolder( addon.location, "", addon, open );
			}
			else if ( EndsWith( lower, ".vpk" ) && !IsVpkArchivePart( lower ) )
				ReadVpk( addon.location, addon );
			else
				continue;

			// Root-level files aren't game content (see WalkFolder).
			std::vector<FileEntry> content;
			for ( size_t f = 0; f < addon.files.size(); f++ )
				if ( addon.files[f].path.find( '/' ) != std::string::npos )
					content.push_back( addon.files[f] );
			addon.files.swap( content );

			addon.loadOrder = FindLoadOrder( addon, loadOrder );
			r.totalFiles += addon.files.size();
			r.addons.push_back( addon );
		}
	}

	// Group every copy by path.
	std::map<std::string, std::vector<Copy> > byPath;
	for ( size_t a = 0; a < r.addons.size(); a++ )
	{
		for ( size_t f = 0; f < r.addons[a].files.size(); f++ )
		{
			Copy c = { a, f };
			byPath[r.addons[a].files[f].path].push_back( c );
		}
	}

	for ( std::map<std::string, std::vector<Copy> >::iterator it = byPath.begin(); it != byPath.end(); ++it )
	{
		std::vector<Copy> &copies = it->second;
		if ( DistinctAddons( copies ) < 2 )
			continue;

		bool identical = true;
		for ( size_t i = 1; i < copies.size(); i++ )
			if ( !SameContents( FileOf( r, copies[0] ), FileOf( r, copies[i] ) ) )
				identical = false;

		PathGroup g;
		g.path = it->first;
		g.copies = copies;

		// Winner first when the search path tells us the order.
		bool allKnown = true;
		for ( size_t i = 0; i < copies.size(); i++ )
			if ( r.addons[copies[i].addon].loadOrder < 0 )
				allKnown = false;
		if ( allKnown )
			std::stable_sort( g.copies.begin(), g.copies.end(), ByLoadOrder( r ) );

		( identical ? r.samePathDupes : r.conflicts ).push_back( g );
	}

	// Most dangerous conflicts first, alphabetical within a severity.
	std::stable_sort( r.conflicts.begin(), r.conflicts.end(), BySeverity() );

	FindMixedModels( byPath, r );

	// Identical contents under different paths in different addons. A group
	// qualifies when it spans two paths and two addons: then some two of its
	// copies differ in both. Same-path copies in it are listed too.
	if ( minDupeSize < 1 )
		minDupeSize = 1;
	std::map<std::pair<uint64_t, uint32_t>, std::vector<Copy> > byContent;
	for ( size_t a = 0; a < r.addons.size(); a++ )
	{
		for ( size_t f = 0; f < r.addons[a].files.size(); f++ )
		{
			const FileEntry &fe = r.addons[a].files[f];
			if ( fe.readable && fe.size >= minDupeSize )
			{
				Copy c = { a, f };
				byContent[std::make_pair( fe.size, fe.crc )].push_back( c );
			}
		}
	}

	for ( std::map<std::pair<uint64_t, uint32_t>, std::vector<Copy> >::iterator it = byContent.begin(); it != byContent.end(); ++it )
	{
		if ( DistinctAddons( it->second ) < 2 || DistinctPaths( r, it->second ) < 2 )
			continue;
		ContentGroup g;
		g.size = it->first.first;
		g.copies = it->second;
		std::sort( g.copies.begin(), g.copies.end(), ByPath( r ) );
		r.contentDupes.push_back( g );
	}

	// Biggest wasted space first.
	std::stable_sort( r.contentDupes.begin(), r.contentDupes.end(), BySize() );

	return r;
}

//------------------------------------------------------------------------
// Report text
//------------------------------------------------------------------------

static std::string NiceSize( uint64_t bytes )
{
	char buf[64];
	if ( bytes >= 1024 * 1024 )
		snprintf( buf, sizeof( buf ), "%.1f MB", bytes / ( 1024.0 * 1024.0 ) );
	else if ( bytes >= 1024 )
		snprintf( buf, sizeof( buf ), "%.1f KB", bytes / 1024.0 );
	else
		snprintf( buf, sizeof( buf ), "%u bytes", (unsigned)bytes );
	return buf;
}

static std::string Num( size_t n )
{
	char buf[32];
	snprintf( buf, sizeof( buf ), "%u", (unsigned)n );
	return buf;
}

static void SectionHeader( std::string &out, const char *title, size_t count, const char *explain )
{
	out += "== ";
	out += title;
	out += " (" + Num( count ) + ") ==\n";
	out += explain;
	out += "\n\n";
}

static bool MoreLeft( std::string &out, size_t index, size_t total, size_t maxPerSection )
{
	if ( maxPerSection && index >= maxPerSection )
	{
		out += "  ... and " + Num( total - index ) + " more (see the full report)\n";
		return false;
	}
	return true;
}

std::string FormatReport( const Report &r, size_t maxPerSection )
{
	std::string out;
	out += "Addon Checker: " + Num( r.addons.size() ) + " addons, " + Num( r.totalFiles ) + " files\n";
	out += Num( r.conflicts.size() ) + " conflicts, " + Num( r.mixedModels.size() ) + " mixed models, " +
	       Num( r.samePathDupes.size() ) + " same-path duplicates, " +
	       Num( r.contentDupes.size() ) + " content duplicates\n\n";

	bool anyErrors = false;
	for ( size_t a = 0; a < r.addons.size(); a++ )
		if ( !r.addons[a].error.empty() )
			anyErrors = true;
	if ( anyErrors )
	{
		out += "== Unreadable addons ==\n";
		for ( size_t a = 0; a < r.addons.size(); a++ )
			if ( !r.addons[a].error.empty() )
				out += "  " + r.addons[a].name + ": " + r.addons[a].error + "\n";
		out += "\n";
	}

	SectionHeader( out, "CONFLICTS", r.conflicts.size(),
		"Same file, different contents. The game only uses one copy, so the other\n"
		"addon loses that file. [!] = scripts/resource/cfg/maps, likely to break\n"
		"things. [MODEL] = part of a model; MIXED MODELS below lists the models\n"
		"that end up built from different addons' parts." );
	for ( size_t i = 0; i < r.conflicts.size(); i++ )
	{
		if ( !MoreLeft( out, i, r.conflicts.size(), maxPerSection ) )
			break;
		const PathGroup &g = r.conflicts[i];
		int sev = Severity( g.path );
		out += sev == 2 ? "  [!] " : sev == 1 ? "  [MODEL] " : "  ";
		out += g.path + "\n";

		bool orderKnown = r.addons[g.copies[0].addon].loadOrder >= 0;
		for ( size_t c = 0; c < g.copies.size(); c++ )
		{
			const Addon &a = r.addons[g.copies[c].addon];
			const FileEntry &fe = FileOf( r, g.copies[c] );
			out += "      " + a.name + "  (" + ( fe.readable ? NiceSize( fe.size ) : std::string( "unreadable" ) ) + ")";
			if ( orderKnown )
				out += c == 0 ? "  <- used" : "  <- ignored";
			out += "\n";
		}
	}
	out += "\n";

	SectionHeader( out, "MIXED MODELS", r.mixedModels.size(),
		"The game takes each part of a model (.mdl .vvd .dx90.vtx .phy .ani) from\n"
		"the first addon that has it, so these models are built from different\n"
		"addons' parts. Mismatched parts make a model invisible or distorted, or\n"
		"crash the game. Keep one addon's full set of parts and remove the rest." );
	for ( size_t i = 0; i < r.mixedModels.size(); i++ )
	{
		if ( !MoreLeft( out, i, r.mixedModels.size(), maxPerSection ) )
			break;
		const ModelGroup &g = r.mixedModels[i];
		out += "  [MODEL] " + g.path;
		if ( !g.orderKnown )
			out += "  (in some load orders)";
		out += "\n";

		for ( size_t p = 0; p < g.parts.size(); p++ )
		{
			const ModelPart &mp = g.parts[p];
			std::string line = "      " + mp.ext;
			line.resize( 18, ' ' );
			if ( g.orderKnown )
			{
				// Winner first, the rest are ignored.
				line += r.addons[mp.copies[0].addon].name;
				for ( size_t c = 1; c < mp.copies.size(); c++ )
					line += ( c == 1 ? "  (ignored: " : ", " ) + r.addons[mp.copies[c].addon].name;
				if ( mp.copies.size() > 1 )
					line += ")";
			}
			else
			{
				for ( size_t c = 0; c < mp.copies.size(); c++ )
					line += ( c ? ", " : "" ) + r.addons[mp.copies[c].addon].name;
			}
			out += line + "\n";
		}
	}
	out += "\n";

	SectionHeader( out, "SAME-PATH DUPLICATES", r.samePathDupes.size(),
		"Same file, identical contents in several addons. Harmless, but you can\n"
		"delete the extra copies." );
	for ( size_t i = 0; i < r.samePathDupes.size(); i++ )
	{
		if ( !MoreLeft( out, i, r.samePathDupes.size(), maxPerSection ) )
			break;
		const PathGroup &g = r.samePathDupes[i];
		out += "  " + g.path + "\n      in: ";
		for ( size_t c = 0; c < g.copies.size(); c++ )
			out += ( c ? ", " : "" ) + r.addons[g.copies[c].addon].name;
		out += "\n";
	}
	out += "\n";

	SectionHeader( out, "CONTENT DUPLICATES", r.contentDupes.size(),
		"Identical file contents under different names in different addons.\n"
		"Harmless, just wasted space. Biggest first." );
	for ( size_t i = 0; i < r.contentDupes.size(); i++ )
	{
		if ( !MoreLeft( out, i, r.contentDupes.size(), maxPerSection ) )
			break;
		const ContentGroup &g = r.contentDupes[i];
		out += "  " + NiceSize( g.size ) + " each:\n";
		for ( size_t c = 0; c < g.copies.size(); c++ )
			out += "      " + FileOf( r, g.copies[c] ).path + "  [" + r.addons[g.copies[c].addon].name + "]\n";
	}

	return out;
}

} // namespace AddonChecker
