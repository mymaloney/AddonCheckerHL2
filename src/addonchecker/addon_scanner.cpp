//========================================================================
// Addon Checker - scanner. See addon_scanner.h.
//========================================================================
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

	std::vector<unsigned char> tree( treeSize );
	bool readOk = treeSize == 0 || fread( &tree[0], 1, treeSize, f ) == treeSize;
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

static void WalkFolder( const std::string &root, const std::string &rel, Addon &addon )
{
	std::vector<DirEntry> entries;
	ListDir( rel.empty() ? root : root + "/" + rel, entries );

	for ( size_t i = 0; i < entries.size(); i++ )
	{
		const DirEntry &e = entries[i];
		std::string relPath = JoinPath( rel, e.name );

		if ( e.isDir )
		{
			WalkFolder( root, relPath, addon );
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
				WalkFolder( addon.location, "", addon );
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

	// Identical contents under different paths. One copy per path is enough;
	// same-path copies are already reported above.
	if ( minDupeSize < 1 )
		minDupeSize = 1;
	std::map<std::pair<uint64_t, uint32_t>, std::vector<Copy> > byContent;
	for ( std::map<std::string, std::vector<Copy> >::iterator it = byPath.begin(); it != byPath.end(); ++it )
	{
		const Copy &c = it->second[0];
		const FileEntry &fe = FileOf( r, c );
		if ( fe.readable && fe.size >= minDupeSize )
			byContent[std::make_pair( fe.size, fe.crc )].push_back( c );
	}

	for ( std::map<std::pair<uint64_t, uint32_t>, std::vector<Copy> >::iterator it = byContent.begin(); it != byContent.end(); ++it )
	{
		if ( it->second.size() < 2 || DistinctAddons( it->second ) < 2 )
			continue;
		ContentGroup g;
		g.size = it->first.first;
		g.copies = it->second;
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
		out += "  ... and " + Num( total - index ) + " more (see addon_report.txt)\n";
		return false;
	}
	return true;
}

std::string FormatReport( const Report &r, size_t maxPerSection )
{
	std::string out;
	out += "Addon Checker: " + Num( r.addons.size() ) + " addons, " + Num( r.totalFiles ) + " files\n";
	out += Num( r.conflicts.size() ) + " conflicts, " + Num( r.samePathDupes.size() ) + " same-path duplicates, " +
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
		"things. [MODEL] = a model's .mdl/.vvd/.vtx/.phy parts coming from different\n"
		"addons can make it invisible, distorted or crash the game." );
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
