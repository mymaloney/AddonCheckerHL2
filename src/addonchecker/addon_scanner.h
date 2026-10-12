//========================================================================
// Addon Checker - scans Source engine custom/ folders for addons that
// overwrite each other's files or ship duplicate copies of the same file.
//
// Plain C++ with no Source SDK dependency, so it can be built into the
// client DLL and also into the standalone command-line tool for testing.
//========================================================================
#ifndef ADDON_SCANNER_H
#define ADDON_SCANNER_H

#include <string>
#include <vector>
#include <stdint.h>

namespace AddonChecker
{

struct FileEntry
{
	std::string path;     // lowercase, forward slashes, relative to the addon root
	uint32_t    crc;      // CRC32 of the contents (same algorithm VPKs store)
	uint64_t    size;
	bool        readable; // false if a loose file couldn't be opened
};

struct Addon
{
	std::string name;     // folder or .vpk file name
	std::string location; // full path on disk
	bool        isVpk;
	int         loadOrder; // position in the engine's search path, -1 if unknown; lower wins
	std::vector<FileEntry> files;
	std::string error;    // set if the addon couldn't be (fully) read
};

struct Copy
{
	size_t addon; // index into Report::addons
	size_t file;  // index into Addon::files
};

struct PathGroup
{
	std::string       path;
	std::vector<Copy> copies; // sorted by load order when it's known
};

struct ContentGroup
{
	uint64_t          size;
	std::vector<Copy> copies;
};

struct Report
{
	std::vector<Addon>        addons;
	std::vector<PathGroup>    conflicts;     // same path, different contents
	std::vector<PathGroup>    samePathDupes; // same path, identical contents
	std::vector<ContentGroup> contentDupes;  // identical contents, different paths
	size_t                    totalFiles;
};

// Scans every addon (sub-folder or .vpk) inside each of customDirs.
// loadOrder is the engine's search path, highest priority first; it is used
// to tell which copy of a conflicting file the game actually uses. Pass an
// empty list if it isn't available.
// Files smaller than minDupeSize are not checked for content duplicates.
Report Scan( const std::vector<std::string> &customDirs,
             const std::vector<std::string> &loadOrder,
             uint64_t minDupeSize );

// maxPerSection limits entries listed per section, 0 = unlimited.
std::string FormatReport( const Report &report, size_t maxPerSection );

// Lowercase, forward slashes, no trailing slash.
std::string NormalizePath( const std::string &path );

} // namespace AddonChecker

#endif // ADDON_SCANNER_H
