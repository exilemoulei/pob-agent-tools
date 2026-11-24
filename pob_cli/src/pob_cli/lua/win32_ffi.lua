-- Windows FFI platform & zlib1.dll bindings for headless LuaJIT host.
-- Exposes M.install(game_dir) -> platform helper table M and installs PoB host globals:
--   GetTime, GetWorkDir, SetWorkDir, MakeDir, RemoveDir, NewFileSearch, Inflate, Deflate.

local ffi = require("ffi")
local bit = require("bit")

local M = {}
local _cdef_done = false

local function ensure_cdef()
	if _cdef_done then
		return
	end
	ffi.cdef[[
		typedef int WIN_BOOL;
		typedef unsigned long WIN_DWORD;
		typedef unsigned short WIN_WORD;
		typedef unsigned short WIN_WCHAR;
		typedef int64_t WIN_LONGLONG;
		typedef void* WIN_HANDLE;

		typedef struct {
			WIN_DWORD dwLowDateTime;
			WIN_DWORD dwHighDateTime;
		} WIN_FILETIME;

		typedef struct {
			WIN_DWORD dwFileAttributes;
			WIN_FILETIME ftCreationTime;
			WIN_FILETIME ftLastAccessTime;
			WIN_FILETIME ftLastWriteTime;
			WIN_DWORD nFileSizeHigh;
			WIN_DWORD nFileSizeLow;
			WIN_DWORD dwReserved0;
			WIN_DWORD dwReserved1;
			WIN_WCHAR cFileName[260];
			WIN_WCHAR cAlternateFileName[14];
		} WIN_FIND_DATAW;

		typedef struct {
			WIN_DWORD dwFileAttributes;
			WIN_FILETIME ftCreationTime;
			WIN_FILETIME ftLastAccessTime;
			WIN_FILETIME ftLastWriteTime;
			WIN_DWORD nFileSizeHigh;
			WIN_DWORD nFileSizeLow;
		} WIN_FILE_ATTR_DATA;

		int MultiByteToWideChar(unsigned int CodePage, WIN_DWORD dwFlags,
			const char *lpMultiByteStr, int cbMultiByte,
			WIN_WCHAR *lpWideCharStr, int cchWideChar);
		int WideCharToMultiByte(unsigned int CodePage, WIN_DWORD dwFlags,
			const WIN_WCHAR *lpWideCharStr, int cchWideChar,
			char *lpMultiByteStr, int cbMultiByte,
			const char *lpDefaultChar, WIN_BOOL *lpUsedDefaultChar);

		WIN_BOOL QueryPerformanceCounter(WIN_LONGLONG *lpPerformanceCount);
		WIN_BOOL QueryPerformanceFrequency(WIN_LONGLONG *lpFrequency);
		WIN_BOOL CreateDirectoryW(const WIN_WCHAR *lpPathName, void *lpSecurityAttributes);
		WIN_BOOL RemoveDirectoryW(const WIN_WCHAR *lpPathName);
		WIN_BOOL DeleteFileW(const WIN_WCHAR *lpFileName);
		WIN_BOOL SetCurrentDirectoryW(const WIN_WCHAR *lpPathName);
		WIN_DWORD GetCurrentDirectoryW(WIN_DWORD nBufferLength, WIN_WCHAR *lpBuffer);
		WIN_DWORD GetFileAttributesW(const WIN_WCHAR *lpFileName);
		WIN_BOOL GetFileAttributesExW(const WIN_WCHAR *lpFileName, int fInfoLevelId, void *lpFileInformation);
		WIN_HANDLE FindFirstFileW(const WIN_WCHAR *lpFileName, WIN_FIND_DATAW *lpFindFileData);
		WIN_BOOL FindNextFileW(WIN_HANDLE hFindFile, WIN_FIND_DATAW *lpFindFileData);
		WIN_BOOL FindClose(WIN_HANDLE hFindFile);

		typedef struct pob_z_stream_s {
			const unsigned char *next_in;
			unsigned int avail_in;
			unsigned long total_in;
			unsigned char *next_out;
			unsigned int avail_out;
			unsigned long total_out;
			const char *msg;
			void *state;
			void *zalloc;
			void *zfree;
			void *opaque;
			int data_type;
			unsigned long adler;
			unsigned long reserved;
		} pob_z_stream;

		const char *zlibVersion(void);
		int inflateInit2_(pob_z_stream *strm, int windowBits, const char *version, int stream_size);
		int inflate(pob_z_stream *strm, int flush);
		int inflateEnd(pob_z_stream *strm);
		int deflateInit2_(pob_z_stream *strm, int level, int method, int windowBits,
			int memLevel, int strategy, const char *version, int stream_size);
		int deflate(pob_z_stream *strm, int flush);
		int deflateEnd(pob_z_stream *strm);
	]]
	_cdef_done = true
end

local CP_UTF8 = 65001
local FILE_ATTR_DIRECTORY = 0x10
local FILE_ATTR_REPARSE_POINT = 0x400
local INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

local function utf8_to_wide(s)
	if type(s) ~= "string" then
		s = tostring(s or "")
	end
	local n = ffi.C.MultiByteToWideChar(CP_UTF8, 0, s, #s, nil, 0)
	local buf = ffi.new("WIN_WCHAR[?]", n + 1)
	if n > 0 then
		ffi.C.MultiByteToWideChar(CP_UTF8, 0, s, #s, buf, n)
	end
	buf[n] = 0
	return buf
end

local function wide_to_utf8(wbuf, wlen)
	wlen = wlen or -1
	local n = ffi.C.WideCharToMultiByte(CP_UTF8, 0, wbuf, wlen, nil, 0, nil, nil)
	if n <= 0 then
		return ""
	end
	local buf = ffi.new("char[?]", n + 1)
	ffi.C.WideCharToMultiByte(CP_UTF8, 0, wbuf, wlen, buf, n, nil, nil)
	if wlen == -1 then
		return ffi.string(buf, n - 1)
	end
	return ffi.string(buf, n)
end

local function strip_trailing_slash(p)
	if not p or p == "" then
		return "."
	end
	local s = p:gsub("\\", "/")
	if s:match("^%a:/$") or s == "/" then
		return s
	end
	return (s:gsub("/+$", ""))
end

local function filetime_to_unix(ft)
	local ticks = tonumber(ft.dwHighDateTime) * 4294967296.0 + tonumber(ft.dwLowDateTime)
	local sec = ticks / 10000000.0 - 11644473600.0
	if sec < 0 then
		return 0
	end
	return sec
end

local function load_zlib(game_dir)
	local candidates = {
		game_dir .. "/zlib1.dll",
		"zlib1.dll",
		"zlib1",
		"z",
	}
	for _, name in ipairs(candidates) do
		local ok, lib = pcall(ffi.load, name)
		if ok and lib then
			return lib
		end
	end
	error("win32_ffi: unable to load zlib1.dll from '" .. tostring(game_dir) .. "'")
end

function M.install(game_dir)
	ensure_cdef()
	local gdir = strip_trailing_slash(tostring(game_dir or "."))
	local kernel32 = ffi.C
	local invalid_handle = ffi.cast("WIN_HANDLE", -1)

	-- Ensure PoE1 DLLs (e.g. lua-utf8.dll) are discoverable via require()
	local cpath_entry = gdir .. "/?.dll"
	if not (package.cpath or ""):find(cpath_entry, 1, true) then
		package.cpath = cpath_entry .. ";" .. (package.cpath or "")
	end

	-- Monotonic high-resolution QPC clock (ms)
	local qpc_freq = ffi.new("WIN_LONGLONG[1]")
	local qpc_start = ffi.new("WIN_LONGLONG[1]")
	local qpc_now = ffi.new("WIN_LONGLONG[1]")
	kernel32.QueryPerformanceFrequency(qpc_freq)
	kernel32.QueryPerformanceCounter(qpc_start)
	local qpc_div = tonumber(qpc_freq[0]) / 1000.0
	if qpc_div <= 0 then
		qpc_div = 1.0
	end

	local function win_get_time()
		kernel32.QueryPerformanceCounter(qpc_now)
		return math.floor(tonumber(qpc_now[0] - qpc_start[0]) / qpc_div)
	end

	local function win_is_dir(path)
		if type(path) ~= "string" or path == "" then
			return false
		end
		local attrs = kernel32.GetFileAttributesW(utf8_to_wide(path))
		if attrs == INVALID_FILE_ATTRIBUTES then
			return false
		end
		return bit.band(attrs, FILE_ATTR_DIRECTORY) ~= 0
	end

	local function win_getcwd()
		local buf = ffi.new("WIN_WCHAR[4096]")
		local n = kernel32.GetCurrentDirectoryW(4096, buf)
		if n == 0 or n >= 4096 then
			return gdir
		end
		return wide_to_utf8(buf, n):gsub("\\", "/")
	end

	local function win_chdir(path)
		if type(path) == "string" and path ~= "" then
			kernel32.SetCurrentDirectoryW(utf8_to_wide(path))
		end
	end

	local function win_mkdir_p(path)
		if type(path) ~= "string" or path == "" then
			return nil, "empty path"
		end
		local norm = strip_trailing_slash(path)
		if win_is_dir(norm) then
			return true
		end
		local acc, rest = "", norm
		local unc_root, unc_tail = norm:match("^(//[^/]+/[^/]+)(.*)$")
		if unc_root then
			acc, rest = unc_root, unc_tail or ""
		elseif norm:sub(1, 1) == "/" then
			acc, rest = "/", norm:sub(2)
		end
		for seg in rest:gmatch("[^/]+") do
			acc = (acc == "" and seg) or (acc == "/" and ("/" .. seg)) or (acc .. "/" .. seg)
			if not acc:match("^%a:$") and not win_is_dir(acc) then
				local ok = kernel32.CreateDirectoryW(utf8_to_wide(acc), nil)
				if ok == 0 and not win_is_dir(acc) then
					return nil, "mkdir failed: " .. acc
				end
			end
		end
		return true
	end

	local function win_file_mtime(path)
		if type(path) ~= "string" or path == "" then
			return 0
		end
		local info = ffi.new("WIN_FILE_ATTR_DATA")
		if kernel32.GetFileAttributesExW(utf8_to_wide(path), 0, info) == 0 then
			return 0
		end
		return filetime_to_unix(info.ftLastWriteTime)
	end

	local function win_remove_dir(path, recurse)
		if type(path) ~= "string" or path == "" then
			return false, "empty path"
		end
		local norm = strip_trailing_slash(path)
		if norm == "/" or norm == "." or norm == ".." or norm:match("^%a:/?$") then
			return false, "refusing to remove root/dot directory"
		end
		local top_attr = kernel32.GetFileAttributesW(utf8_to_wide(norm))
		local is_reparse = (top_attr ~= INVALID_FILE_ATTRIBUTES)
			and (bit.band(top_attr, FILE_ATTR_REPARSE_POINT) ~= 0)
		if recurse and not is_reparse then
			local fd = ffi.new("WIN_FIND_DATAW")
			local h = kernel32.FindFirstFileW(utf8_to_wide(norm .. "/*"), fd)
			if h ~= invalid_handle then
				repeat
					local name = wide_to_utf8(fd.cFileName)
					if name ~= "." and name ~= ".." then
						local child = norm .. "/" .. name
						local attr = fd.dwFileAttributes
						if bit.band(attr, FILE_ATTR_DIRECTORY) ~= 0 then
							local ok, err
							if bit.band(attr, FILE_ATTR_REPARSE_POINT) ~= 0 then
								ok = kernel32.RemoveDirectoryW(utf8_to_wide(child)) ~= 0
								err = not ok and ("rmdir failed: " .. child) or nil
							else
								ok, err = win_remove_dir(child, true)
							end
							if not ok then
								kernel32.FindClose(h)
								return false, err
							end
						else
							kernel32.DeleteFileW(utf8_to_wide(child))
						end
					end
				until kernel32.FindNextFileW(h, fd) == 0
				kernel32.FindClose(h)
			end
		end
		if kernel32.RemoveDirectoryW(utf8_to_wide(norm)) ~= 0 then
			return true
		end
		return false, "rmdir failed: " .. norm
	end

	local function win_new_file_search(pattern, dirs_only)
		if type(pattern) ~= "string" or pattern == "" then
			return nil
		end
		local want_dirs = dirs_only and true or false
		local find_data = ffi.new("WIN_FIND_DATAW")
		local handle = kernel32.FindFirstFileW(utf8_to_wide(pattern), find_data)
		if handle == invalid_handle then
			return nil
		end

		local function valid_entry()
			local name = wide_to_utf8(find_data.cFileName)
			if name == "." or name == ".." then
				return false
			end
			local is_dir = bit.band(find_data.dwFileAttributes, FILE_ATTR_DIRECTORY) ~= 0
			return is_dir == want_dirs
		end

		while not valid_entry() do
			if kernel32.FindNextFileW(handle, find_data) == 0 then
				kernel32.FindClose(handle)
				return nil
			end
		end

		local closed = false
		local gc_owner = ffi.gc(handle, function(h)
			if not closed then
				closed = true
				kernel32.FindClose(h)
			end
		end)
		local obj = { _gc = gc_owner }
		function obj:NextFile()
			if closed then
				return false
			end
			while true do
				if kernel32.FindNextFileW(handle, find_data) == 0 then
					closed = true
					ffi.gc(gc_owner, nil)
					kernel32.FindClose(handle)
					return false
				end
				if valid_entry() then
					return true
				end
			end
		end
		function obj:GetFileName()
			return wide_to_utf8(find_data.cFileName)
		end
		function obj:GetFileSize()
			return tonumber(find_data.nFileSizeHigh) * 4294967296.0 + tonumber(find_data.nFileSizeLow)
		end
		function obj:GetFileModifiedTime()
			return filetime_to_unix(find_data.ftLastWriteTime)
		end
		return obj
	end

	-- zlib1.dll Inflate / Deflate
	local zlib = load_zlib(gdir)
	local z_ver = zlib.zlibVersion()
	local z_stream_size = ffi.sizeof("pob_z_stream")
	local Z_OK, Z_STREAM_END, Z_FINISH, Z_NO_FLUSH, Z_BUF_ERROR = 0, 1, 4, 0, -5

	local function zlib_inflate_wbits(data, wbits)
		local strm = ffi.new("pob_z_stream")
		if zlib.inflateInit2_(strm, wbits, z_ver, z_stream_size) ~= Z_OK then
			return nil, "inflateInit2 failed"
		end
		strm.next_in = ffi.cast("const unsigned char *", data)
		strm.avail_in = #data
		local chunk_size = 65536
		local buf = ffi.new("unsigned char[?]", chunk_size)
		local parts = {}
		local rc = Z_OK
		while true do
			strm.next_out = buf
			strm.avail_out = chunk_size
			rc = zlib.inflate(strm, Z_NO_FLUSH)
			local produced = chunk_size - tonumber(strm.avail_out)
			if produced > 0 then
				parts[#parts + 1] = ffi.string(buf, produced)
			end
			if rc == Z_STREAM_END then
				break
			elseif rc ~= Z_OK then
				zlib.inflateEnd(strm)
				return nil, "inflate failed: " .. tostring(rc)
			end
			if tonumber(strm.avail_in) == 0 and produced == 0 then
				zlib.inflateEnd(strm)
				return nil, "inflate truncated input"
			end
		end
		zlib.inflateEnd(strm)
		return table.concat(parts)
	end

	local function win_inflate(data)
		if type(data) ~= "string" then
			return nil, "Inflate: expected string"
		end
		if #data == 0 then
			return ""
		end
		local out, err = zlib_inflate_wbits(data, 47) -- auto zlib/gzip
		if out ~= nil then
			return out
		end
		local raw_out = zlib_inflate_wbits(data, -15) -- raw deflate fallback
		if raw_out ~= nil then
			return raw_out
		end
		return nil, err
	end

	local function win_deflate(data, is_gzip)
		if type(data) ~= "string" then
			return nil, "Deflate: expected string"
		end
		local strm = ffi.new("pob_z_stream")
		local wbits = is_gzip and (15 + 16) or 15
		if zlib.deflateInit2_(strm, -1, 8, wbits, 8, 0, z_ver, z_stream_size) ~= Z_OK then
			return nil, "deflateInit2 failed"
		end
		strm.next_in = ffi.cast("const unsigned char *", data)
		strm.avail_in = #data
		local chunk_size = 65536
		local buf = ffi.new("unsigned char[?]", chunk_size)
		local parts = {}
		while true do
			strm.next_out = buf
			strm.avail_out = chunk_size
			local rc = zlib.deflate(strm, Z_FINISH)
			local produced = chunk_size - tonumber(strm.avail_out)
			if produced > 0 then
				parts[#parts + 1] = ffi.string(buf, produced)
			end
			if rc == Z_STREAM_END then
				break
			elseif rc ~= Z_OK and rc ~= Z_BUF_ERROR then
				zlib.deflateEnd(strm)
				return nil, "deflate failed: " .. tostring(rc)
			end
		end
		zlib.deflateEnd(strm)
		return table.concat(parts)
	end

	-- Switch cwd to game_dir immediately so relative PoB loads work
	win_chdir(gdir)

	-- Install SimpleGraphic host globals
	GetTime = win_get_time
	GetWorkDir = win_getcwd
	SetWorkDir = win_chdir
	MakeDir = win_mkdir_p
	RemoveDir = win_remove_dir
	NewFileSearch = win_new_file_search
	Inflate = win_inflate
	Deflate = win_deflate

	M.chdir = win_chdir
	M.getcwd = win_getcwd
	M.mkdir_p = win_mkdir_p
	M.remove_dir = win_remove_dir
	M.file_mtime = win_file_mtime
	M.is_dir = win_is_dir
	M.get_time = win_get_time
	M.new_file_search = win_new_file_search
	M.inflate = win_inflate
	M.deflate = win_deflate
	M.installed = true
	return M
end

return M
