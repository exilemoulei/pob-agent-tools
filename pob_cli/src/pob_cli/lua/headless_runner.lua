-- Cross-platform (macOS / Linux) headless runner for Path of Building + PobTools bridge.lua.
--
-- Usage:
--   luajit headless_runner.lua <game_dir> <bridge_lua_path>
--
-- Implements the SimpleGraphic headless surface, LuaJIT FFI bindings to system
-- libz (Inflate/Deflate) and POSIX directory/time calls, then boots Launch.lua
-- and loads PobTools-zh's MIT bridge.lua speaking the exact NDJSON stdin/stdout
-- protocol expected by pob_cli.bridge_client.BridgeWorker.

local ffi = require("ffi")
local bit = require("bit")

local GAME_DIR = arg and arg[1] or "."
local BRIDGE_PATH = arg and arg[2] or ""

-- Strip trailing slashes (except root "/")
local function strip_trailing_slash(p)
	if not p or p == "" then return "." end
	if p == "/" then return "/" end
	return (p:gsub("/+$", ""))
end

GAME_DIR = strip_trailing_slash(GAME_DIR)

-- Reserve raw stdout exclusively for JSON-RPC frames; redirect print/ConPrintf to stderr.
local RAW_STDOUT = io.stdout
local RAW_STDERR = io.stderr

local function log_stderr(msg)
	RAW_STDERR:write(tostring(msg), "\n")
	RAW_STDERR:flush()
end

function print(...)
	local parts = {}
	local n = select("#", ...)
	for i = 1, n do
		parts[i] = tostring(select(i, ...))
	end
	RAW_STDERR:write(table.concat(parts, "\t"), "\n")
	RAW_STDERR:flush()
end

-- Configure Lua search paths before loading PoB modules
local engine_root = os.getenv("POB_ENGINE_ROOT")
if not engine_root or engine_root == "" then
	engine_root = GAME_DIR:match("^(.*)/[^/]+$") or "."
end
engine_root = strip_trailing_slash(engine_root)

package.path = table.concat({
	GAME_DIR .. "/lua/?.lua",
	GAME_DIR .. "/lua/?/init.lua",
	GAME_DIR .. "/?.lua",
	GAME_DIR .. "/?/init.lua",
	package.path or "",
}, ";")

package.cpath = table.concat({
	engine_root .. "/bin/?.so",
	engine_root .. "/bin/?.dylib",
	GAME_DIR .. "/?.so",
	GAME_DIR .. "/?.dylib",
	package.cpath or "",
}, ";")

-- ---------------------------------------------------------------------------
-- POSIX FFI: chdir / getcwd / mkdir / rmdir / unlink / gettimeofday / opendir
-- ---------------------------------------------------------------------------

ffi.cdef[[
	int chdir(const char *path);
	char *getcwd(char *buf, size_t size);
	int mkdir(const char *path, unsigned int mode);
	int rmdir(const char *path);
	int unlink(const char *path);

	typedef struct {
		int64_t tv_sec;
		int32_t tv_usec;
		int32_t _pad;
	} pob_timeval_t;
	int gettimeofday(pob_timeval_t *tv, void *tz);

	void *opendir(const char *name);
	int closedir(void *dirp);
]]

local IS_OSX = (ffi.os == "OSX")
local IS_X64 = (ffi.arch == "x64")

if IS_OSX then
	ffi.cdef[[
		typedef struct {
			uint64_t d_ino;
			uint64_t d_seekoff;
			uint16_t d_reclen;
			uint16_t d_namlen;
			uint8_t  d_type;
			char     d_name[1024];
		} pob_dirent_t;
		/* Darwin 64-bit inode struct stat (x86_64 stat$INODE64 & arm64 stat):
		   dev(4)+mode(2)+nlink(2)+ino(8)+uid(4)+gid(4)+rdev(4)+pad(4)+atime(16) = 48 bytes before st_mtimespec. */
		typedef struct {
			uint8_t _head[48];
			int64_t st_mtime;
			int64_t st_mtime_nsec;
			uint8_t _tail[256];
		} pob_stat_t;
	]]
	if IS_X64 then
		ffi.cdef[[
			pob_dirent_t *pob_readdir(void *dirp) asm("readdir$INODE64");
			int pob_stat(const char *path, pob_stat_t *buf) asm("stat$INODE64");
		]]
	else
		ffi.cdef[[
			pob_dirent_t *pob_readdir(void *dirp) asm("readdir");
			int pob_stat(const char *path, pob_stat_t *buf) asm("stat");
		]]
	end
else
	ffi.cdef[[
		typedef struct {
			uint64_t d_ino;
			int64_t  d_off;
			uint16_t d_reclen;
			uint8_t  d_type;
			char     d_name[256];
		} pob_dirent_t;
		/* Linux x86_64 & aarch64 struct stat both place st_mtim.tv_sec at byte offset 88. */
		typedef struct {
			uint8_t _head[88];
			int64_t st_mtime;
			int64_t st_mtime_nsec;
			uint8_t _tail[256];
		} pob_stat_t;
		pob_dirent_t *pob_readdir(void *dirp) asm("readdir");
		int pob_stat(const char *path, pob_stat_t *buf) asm("stat");
		int pob_xstat(int ver, const char *path, pob_stat_t *buf) asm("__xstat");
	]]
end

local _pob_stat_fn = nil
local _pob_xstat_fn = nil
local _pob_xstat_ver = IS_X64 and 1 or 0

if IS_OSX then
	local ok, fn = pcall(function() return ffi.C.pob_stat end)
	if ok then _pob_stat_fn = fn end
else
	local ok, fn = pcall(function() return ffi.C.pob_stat end)
	if ok then
		_pob_stat_fn = fn
	else
		local ok2, xfn = pcall(function() return ffi.C.pob_xstat end)
		if ok2 then _pob_xstat_fn = xfn end
	end
end

local function posix_file_mtime(path)
	if type(path) ~= "string" or path == "" then
		return 0
	end
	local st = ffi.new("pob_stat_t")
	local rc = -1
	if _pob_stat_fn then
		rc = _pob_stat_fn(path, st)
	elseif _pob_xstat_fn then
		rc = _pob_xstat_fn(_pob_xstat_ver, path, st)
	end
	if rc ~= 0 then
		return 0
	end
	local sec = tonumber(st.st_mtime) or 0
	if sec < 0 then
		return 0
	end
	local nsec = tonumber(st.st_mtime_nsec) or 0
	if nsec >= 0 and nsec < 1000000000 then
		return sec + nsec * 1e-9
	end
	return sec
end

local DT_UNKNOWN = 0
local DT_DIR = 4
local DT_REG = 8
local DT_LNK = 10

local function posix_is_dir(path)
	local d = ffi.C.opendir(path)
	if d ~= nil then
		ffi.C.closedir(d)
		return true
	end
	return false
end

local function posix_getcwd()
	local buf = ffi.new("char[4096]")
	local res = ffi.C.getcwd(buf, 4096)
	if res == nil then return GAME_DIR end
	return ffi.string(res)
end

local function posix_chdir(path)
	if type(path) == "string" and path ~= "" then
		ffi.C.chdir(path)
	end
end

local function posix_mkdir_p(path)
	if type(path) ~= "string" or path == "" then
		return nil, "empty path"
	end
	local norm = path:gsub("\\", "/")
	local is_abs = norm:sub(1, 1) == "/"
	local acc = is_abs and "" or "."
	for seg in norm:gmatch("[^/]+") do
		if acc == "" then
			acc = "/" .. seg
		elseif acc == "." then
			acc = seg
		else
			acc = acc .. "/" .. seg
		end
		if not posix_is_dir(acc) then
			local rc = ffi.C.mkdir(acc, 493) -- 0755
			if rc ~= 0 and not posix_is_dir(acc) then
				return nil, "mkdir failed: " .. acc
			end
		end
	end
	return true
end

local function posix_list_entries(dir_path)
	local entries = {}
	local dp = ffi.C.opendir(dir_path)
	if dp == nil then
		return entries
	end
	while true do
		local ent = ffi.C.pob_readdir(dp)
		if ent == nil then break end
		local name = ffi.string(ent.d_name)
		if name ~= "." and name ~= ".." then
			local full = (dir_path == "/" and "/" or (dir_path .. "/")) .. name
			local dtype = ent.d_type
			local is_dir
			if dtype == DT_DIR then
				is_dir = true
			elseif dtype == DT_REG then
				is_dir = false
			elseif dtype == DT_UNKNOWN or dtype == DT_LNK then
				is_dir = posix_is_dir(full)
			else
				is_dir = false
			end
			entries[#entries + 1] = { name = name, full = full, is_dir = is_dir }
		end
	end
	ffi.C.closedir(dp)
	return entries
end

local function posix_remove_dir(path, recurse)
	if type(path) ~= "string" or path == "" then
		return false, "empty path"
	end
	local norm = strip_trailing_slash(path:gsub("\\", "/"))
	if norm == "/" or norm == "." or norm == ".." then
		return false, "refusing to remove root/dot directory"
	end
	if recurse then
		for _, e in ipairs(posix_list_entries(norm)) do
			if e.is_dir then
				local ok, err = posix_remove_dir(e.full, true)
				if not ok then return false, err end
			else
				ffi.C.unlink(e.full)
			end
		end
	end
	if ffi.C.rmdir(norm) == 0 then
		return true
	end
	return false, "rmdir failed: " .. norm
end

-- Ensure cwd is GAME_DIR right away
posix_chdir(GAME_DIR)

-- ---------------------------------------------------------------------------
-- zlib FFI: Inflate (auto zlib/gzip) & Deflate (zlib or gzip)
-- ---------------------------------------------------------------------------

ffi.cdef[[
	typedef struct z_stream_s {
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
	} z_stream;

	const char *zlibVersion(void);
	int deflateInit2_(z_stream *strm, int level, int method, int windowBits,
	                  int memLevel, int strategy, const char *version, int stream_size);
	int deflate(z_stream *strm, int flush);
	int deflateEnd(z_stream *strm);

	int inflateInit2_(z_stream *strm, int windowBits, const char *version, int stream_size);
	int inflate(z_stream *strm, int flush);
	int inflateEnd(z_stream *strm);
]]

local function load_zlib()
	local candidates = IS_OSX
		and { "z", "/usr/lib/libz.dylib", "libz.dylib" }
		or { "z", "libz.so.1", "libz.so" }
	for _, name in ipairs(candidates) do
		local ok, lib = pcall(ffi.load, name)
		if ok and lib then
			return lib
		end
	end
	error("Unable to load system zlib library via LuaJIT FFI")
end

local ZLIB = load_zlib()
local ZLIB_VER = ZLIB.zlibVersion()
local Z_STREAM_SIZE = ffi.sizeof("z_stream")
local Z_OK = 0
local Z_STREAM_END = 1
local Z_FINISH = 4
local Z_DEFLATED = 8
local Z_DEFAULT_STRATEGY = 0
local Z_DEFAULT_COMPRESSION = -1

function Deflate(data, isGzip)
	if type(data) ~= "string" then
		return nil, "Deflate: expected string"
	end
	local strm = ffi.new("z_stream")
	local window_bits = isGzip and (15 + 16) or 15
	local rc = ZLIB.deflateInit2_(
		strm, Z_DEFAULT_COMPRESSION, Z_DEFLATED, window_bits, 8, Z_DEFAULT_STRATEGY,
		ZLIB_VER, Z_STREAM_SIZE
	)
	if rc ~= Z_OK then
		return nil, "deflateInit2 failed: " .. tostring(rc)
	end

	local in_len = #data
	local in_buf = ffi.cast("const unsigned char *", data)
	strm.next_in = in_buf
	strm.avail_in = in_len

	local chunk_size = 65536
	local out_buf = ffi.new("unsigned char[?]", chunk_size)
	local chunks = {}

	while true do
		strm.next_out = out_buf
		strm.avail_out = chunk_size
		rc = ZLIB.deflate(strm, Z_FINISH)
		local produced = chunk_size - tonumber(strm.avail_out)
		if produced > 0 then
			chunks[#chunks + 1] = ffi.string(out_buf, produced)
		end
		if rc == Z_STREAM_END then
			break
		elseif rc ~= Z_OK then
			ZLIB.deflateEnd(strm)
			return nil, "deflate failed: " .. tostring(rc)
		end
	end

	ZLIB.deflateEnd(strm)
	return table.concat(chunks)
end

function Inflate(data)
	if type(data) ~= "string" then
		return nil, "Inflate: expected string"
	end
	if #data == 0 then
		return ""
	end
	local strm = ffi.new("z_stream")
	-- 15 + 32 enables automatic zlib and gzip header detection (matches SimpleGraphic)
	local rc = ZLIB.inflateInit2_(strm, 15 + 32, ZLIB_VER, Z_STREAM_SIZE)
	if rc ~= Z_OK then
		return nil, "inflateInit2 failed: " .. tostring(rc)
	end

	local in_buf = ffi.cast("const unsigned char *", data)
	strm.next_in = in_buf
	strm.avail_in = #data

	local chunk_size = 65536
	local out_buf = ffi.new("unsigned char[?]", chunk_size)
	local chunks = {}

	while true do
		strm.next_out = out_buf
		strm.avail_out = chunk_size
		rc = ZLIB.inflate(strm, 0) -- Z_NO_FLUSH
		local produced = chunk_size - tonumber(strm.avail_out)
		if produced > 0 then
			chunks[#chunks + 1] = ffi.string(out_buf, produced)
		end
		if rc == Z_STREAM_END then
			break
		elseif rc ~= Z_OK then
			-- Retry raw deflate (-15) if header detection fails on legacy blobs
			ZLIB.inflateEnd(strm)
			return nil, "inflate failed: " .. tostring(rc)
		end
		if tonumber(strm.avail_in) == 0 and produced == 0 then
			ZLIB.inflateEnd(strm)
			return nil, "inflate truncated input"
		end
	end

	ZLIB.inflateEnd(strm)
	return table.concat(chunks)
end

-- ---------------------------------------------------------------------------
-- JSON serialization matching PobTools headless_bridge.cpp semantics exactly
-- ---------------------------------------------------------------------------

local ESCAPES = {
	['"'] = '\\"',
	['\\'] = '\\\\',
	['\b'] = '\\b',
	['\f'] = '\\f',
	['\n'] = '\\n',
	['\r'] = '\\r',
	['\t'] = '\\t',
}

local function json_escape_str(s)
	return '"' .. s:gsub('[%z\1-\31\\"]', function(c)
		return ESCAPES[c] or string.format("\\u%04x", c:byte())
	end) .. '"'
end

local function is_json_array(tbl)
	local mt = getmetatable(tbl)
	if type(mt) == "table" and mt.__object then
		return false, 0
	end
	local n = #tbl
	local count = 0
	for k in pairs(tbl) do
		count = count + 1
		if type(k) ~= "number" or k ~= math.floor(k) or k < 1 or k > n then
			return false, 0
		end
	end
	return count == n, n
end

local function to_json(val, depth)
	depth = depth or 0
	local t = type(val)
	if t == "nil" then
		return "null"
	elseif t == "boolean" then
		return val and "true" or "false"
	elseif t == "number" then
		if val ~= val or val == math.huge or val == -math.huge then
			return "null"
		end
		if val == math.floor(val) and math.abs(val) < 9007199254740992.0 then
			return string.format("%.0f", val)
		end
		return string.format("%.14g", val)
	elseif t == "string" then
		return json_escape_str(val)
	elseif t == "table" then
		if depth > 32 then
			return '"<too deep>"'
		end
		local is_arr, n = is_json_array(val)
		if is_arr then
			local parts = {}
			for i = 1, n do
				parts[i] = to_json(val[i], depth + 1)
			end
			return "[" .. table.concat(parts, ",") .. "]"
		else
			local parts = {}
			for k, v in pairs(val) do
				local vt = type(v)
				if vt ~= "function" and vt ~= "userdata" and vt ~= "thread" then
					local kt = type(k)
					local kstr
					if kt == "string" then
						kstr = k
					elseif kt == "number" then
						if k == math.floor(k) and math.abs(k) < 9007199254740992.0 then
							kstr = string.format("%.0f", k)
						else
							kstr = string.format("%.14g", k)
						end
					end
					if kstr ~= nil then
						parts[#parts + 1] = json_escape_str(kstr) .. ":" .. to_json(v, depth + 1)
					end
				end
			end
			return "{" .. table.concat(parts, ",") .. "}"
		end
	end
	return "null"
end

local function send_raw_json(line)
	RAW_STDOUT:write(line, "\n")
	RAW_STDOUT:flush()
end

local function send_event(ev, data)
	local payload = (data == nil) and "null" or to_json(data, 0)
	send_raw_json('{"event":' .. json_escape_str(tostring(ev)) .. ',"data":' .. payload .. '}')
end

local function send_result(id, res)
	local payload = (res == nil) and "null" or to_json(res, 0)
	send_raw_json('{"id":' .. to_json(id, 0) .. ',"result":' .. payload .. '}')
end

local function send_error(id, code, message)
	send_raw_json(
		'{"id":' .. to_json(id, 0) ..
		',"error":{"code":' .. json_escape_str(tostring(code)) ..
		',"message":' .. json_escape_str(tostring(message)) .. '}}'
	)
end

-- ---------------------------------------------------------------------------
-- SimpleGraphic headless API stubs
-- ---------------------------------------------------------------------------

local SCREEN_W = tonumber(os.getenv("POB_ZH_HEADLESS_W")) or 1920
local SCREEN_H = tonumber(os.getenv("POB_ZH_HEADLESS_H")) or 1080
local START_TV = ffi.new("pob_timeval_t")
ffi.C.gettimeofday(START_TV, nil)

__callbackTable__ = {}
__mainObject__ = nil

function SetCallback(name, func)
	__callbackTable__[name] = func
end

function GetCallback(name)
	return __callbackTable__[name]
end

function SetMainObject(object)
	__mainObject__ = object
end

local function runCallback(name, ...)
	if __callbackTable__[name] then
		return __callbackTable__[name](...)
	elseif __mainObject__ and __mainObject__[name] then
		return __mainObject__[name](__mainObject__, ...)
	end
end

local imageHandleClass = {}
imageHandleClass.__index = imageHandleClass
function NewImageHandle() return setmetatable({ valid = false }, imageHandleClass) end
function imageHandleClass:Load() self.valid = true end
function imageHandleClass:LoadArtRectangle() self.valid = true end
function imageHandleClass:LoadArtArcBand() self.valid = true end
function imageHandleClass:Unload() self.valid = false end
function imageHandleClass:IsValid() return self.valid and true or false end
function imageHandleClass:IsLoading() return false end
function imageHandleClass:SetLoadingPriority() end
function imageHandleClass:ImageSize() return 1, 1 end

local artHandleClass = {}
artHandleClass.__index = artHandleClass
function artHandleClass:Size() return 1, 1 end
function NewArtHandle() return setmetatable({}, artHandleClass) end

local texHandleClass = {}
texHandleClass.__index = texHandleClass
function texHandleClass:Allocate() return true end
function texHandleClass:Load() return true end
function texHandleClass:Save() return true end
function texHandleClass:Info() return { formatStr = "RGBA", width = 1, height = 1, layerCount = 1, mipCount = 1 } end
function texHandleClass:IsValid() return true end
function texHandleClass:StackTextures() return true end
Texture = { new = function() return setmetatable({}, texHandleClass) end }

function GetScreenSize() return SCREEN_W, SCREEN_H end
function GetVirtualScreenSize() return SCREEN_W, SCREEN_H end
function GetScreenScale() return 1 end
function SetClearColor() end
function SetDrawLayer() end
function GetDrawLayer() return 0, 0 end
function SetViewport() end
function SetBlendMode() end
function SetDrawColor() end
function GetDrawColor() return 1, 1, 1, 1 end
function SetDPIScaleOverridePercent() end
function GetDPIScaleOverridePercent() return 0 end
function DrawImage() end
function DrawImageQuad() end
function DrawString() end
function DrawStringCursorIndex() return 1 end
function GetAsyncCount() return 0 end
function RenderInit() end
function GetCloudProvider() return nil, nil, nil end
function SetWindowTitle() end
function GetCursorPos() return 0, 0 end
function SetCursorPos() end
function ShowCursor() end
function IsKeyDown() return false end
function Copy() end
function Paste() return nil end
function ConPrintTable() end
function ConExecute() end
function ConClear() end
function SpawnProcess() end
function OpenURL() end
function SetProfiling() end
function TakeScreenshot() end
function Restart() end
function Exit() end
function SetForeground() end
function LaunchSubScript() return nil end
function AbortSubScript() end
function IsSubScriptRunning() return false end

function StripEscapes(text)
	if type(text) ~= "string" then return "" end
	local s = text:gsub("%^%d", ""):gsub("%^x%x%x%x%x%x%x", "")
	return s
end

-- Proportional width estimator matching PobTools headless_bridge.cpp
function DrawStringWidth(height, font, text)
	if type(text) ~= "string" then return 0 end
	local s = StripEscapes(text)
	local w = 0
	local i = 1
	local len = #s
	while i <= len do
		local b = s:byte(i)
		if b < 0x80 then
			w = w + 0.55
			i = i + 1
		elseif bit.band(b, 0xE0) == 0xC0 then
			w = w + 0.6
			i = i + 2
		elseif bit.band(b, 0xF0) == 0xE0 then
			w = w + 1.0
			i = i + 3
		elseif bit.band(b, 0xF8) == 0xF0 then
			w = w + 1.0
			i = i + 4
		else
			i = i + 1
		end
	end
	return w * (tonumber(height) or 14)
end

function GetTime()
	local tv = ffi.new("pob_timeval_t")
	ffi.C.gettimeofday(tv, nil)
	local ds = tonumber(tv.tv_sec - START_TV.tv_sec)
	local dus = tonumber(tv.tv_usec - START_TV.tv_usec)
	return math.floor(ds * 1000 + dus / 1000)
end

function GetScriptPath() return GAME_DIR end
function GetRuntimePath() return GAME_DIR end
function GetUserPath() return GAME_DIR end
function GetWorkDir() return posix_getcwd() end
function SetWorkDir(path) posix_chdir(path) end
function MakeDir(path) return posix_mkdir_p(path) end
function RemoveDir(path, recurse) return posix_remove_dir(path, recurse) end

-- Glob-to-Lua-pattern helper for NewFileSearch
local function glob_to_pattern(glob)
	local out = { "^" }
	for i = 1, #glob do
		local c = glob:sub(i, i)
		if c == "*" then
			out[#out + 1] = ".*"
		elseif c == "?" then
			out[#out + 1] = "."
		elseif c:match("[%(%)%.%%%+%-%[%]%^%$]") then
			out[#out + 1] = "%" .. c
		else
			out[#out + 1] = c
		end
	end
	out[#out + 1] = "$"
	return table.concat(out)
end

local fileSearchClass = {}
fileSearchClass.__index = fileSearchClass

function fileSearchClass:NextFile()
	self._idx = self._idx + 1
	return self._idx <= #self._items
end

function fileSearchClass:GetFileName()
	local it = self._items[self._idx]
	return it and it.name or nil
end

function fileSearchClass:GetFileSize()
	local it = self._items[self._idx]
	if not it or it.is_dir then return 0 end
	local f = io.open(it.full, "rb")
	if not f then return 0 end
	local sz = f:seek("end") or 0
	f:close()
	return sz
end

function fileSearchClass:GetFileModifiedTime()
	local it = self._items[self._idx]
	return it and posix_file_mtime(it.full) or 0
end

function NewFileSearch(spec, findDirectories)
	if type(spec) ~= "string" or spec == "" then
		return nil
	end
	local norm = spec:gsub("\\", "/")
	local dir_part, pat_part = norm:match("^(.*)/([^/]*)$")
	if not dir_part then
		dir_part = "."
		pat_part = norm
	elseif dir_part == "" then
		dir_part = "/"
	end
	if pat_part == "" then
		pat_part = "*"
	end
	local want_dir = findDirectories and true or false
	local lua_pat = glob_to_pattern(pat_part)
	local matched = {}
	for _, entry in ipairs(posix_list_entries(dir_part)) do
		if entry.is_dir == want_dir and entry.name:match(lua_pat) then
			matched[#matched + 1] = entry
		end
	end
	if #matched == 0 then
		return nil
	end
	return setmetatable({ _items = matched, _idx = 1 }, fileSearchClass)
end

function PCall(func, ...)
	local ret = { pcall(func, ...) }
	if ret[1] then
		table.remove(ret, 1)
		return nil, unpack(ret)
	else
		return ret[2]
	end
end

function LoadModule(name, ...)
	if not name:match("%.lua$") then
		name = name .. ".lua"
	end
	local func, err = loadfile(name)
	if func then
		return func(...)
	else
		error("LoadModule() error loading '" .. name .. "': " .. tostring(err))
	end
end

function PLoadModule(modName, ...)
	if not modName:match("%.lua$") then
		modName = modName .. ".lua"
	end
	local func, err = loadfile(modName)
	if func then
		return PCall(func, ...)
	else
		error("PLoadModule() error loading '" .. modName .. "': " .. tostring(err))
	end
end

function ConPrintf(fmt, ...)
	local ok, msg = pcall(string.format, tostring(fmt), ...)
	log_stderr(ok and msg or tostring(fmt))
end

-- Stub out lcurl.safe (network I/O is handled in Python per R6/R7)
local orig_require = require
function require(name)
	if name == "lcurl.safe" then
		return nil
	end
	return orig_require(name)
end

-- ---------------------------------------------------------------------------
-- PobTools bridge host hooks
-- ---------------------------------------------------------------------------

local bridge_dispatcher = nil

function PobToolsBridgeSetDispatcher(fn)
	if type(fn) ~= "function" then
		error("PobToolsBridgeSetDispatcher expects a function")
	end
	bridge_dispatcher = fn
end

function PobToolsBridgeEmit(event, data)
	send_event(event, data)
end

function PobToolsHeadless()
	return true
end

function PobToolsGame()
	return os.getenv("POB_GAME") or "poe1"
end

function PobToolsLogError(tag, msg)
	log_stderr("[" .. tostring(tag) .. "] " .. tostring(msg))
end

-- ---------------------------------------------------------------------------
-- Boot PoB Launch.lua, then mount PobTools bridge.lua
-- ---------------------------------------------------------------------------

local dkjson = require("dkjson")

-- Prevent PoB Main.lua:Init() from treating runner CLI args (arg[1]=<game_dir>) as a pob:// import URI.
arg = {}

dofile("Launch.lua")
runCallback("OnInit")
runCallback("OnFrame")

if __mainObject__ and __mainObject__.promptMsg then
	send_event("gate_result", {
		ok = false,
		failed = { "PoB Launch promptMsg: " .. tostring(__mainObject__.promptMsg) },
	})
	os.exit(1)
end

if BRIDGE_PATH == "" then
	BRIDGE_PATH = engine_root .. "/Data/bridge/bridge.lua"
end

local bridge_chunk, bridge_load_err = loadfile(BRIDGE_PATH)
if not bridge_chunk then
	send_event("gate_result", {
		ok = false,
		failed = { "bridge.lua load failed (" .. BRIDGE_PATH .. "): " .. tostring(bridge_load_err) },
	})
	os.exit(1)
end

local ok_bridge, bridge_run_err = xpcall(bridge_chunk, debug.traceback)
if not ok_bridge then
	send_event("gate_result", {
		ok = false,
		failed = { "bridge.lua runtime error: " .. tostring(bridge_run_err) },
	})
	os.exit(1)
end

local RUNNER_DIR = ((debug.getinfo(1, "S") and debug.getinfo(1, "S").source) or ""):match("^@?(.*)[/\\][^/\\]+$") or "."
local import_char_json_fn = nil
do
	local chunk = loadfile(RUNNER_DIR .. "/import_char_json.lua")
	if chunk then
		local ok_mod, mod_fn = pcall(chunk)
		if ok_mod and type(mod_fn) == "function" then import_char_json_fn = mod_fn end
	end
end

-- ---------------------------------------------------------------------------
-- Blocking NDJSON stdin/stdout RPC loop
-- ---------------------------------------------------------------------------

while true do
	local line = io.stdin:read("*l")
	if not line then
		break
	end
	line = line:gsub("\r$", "")
	if line ~= "" then
		local req, _, dec_err = dkjson.decode(line, 1, nil)
		if type(req) ~= "table" then
			send_error(-1, "bad_json", dec_err or "invalid JSON")
		else
			local id = req.id
			if id == nil then id = -1 end
			local method = req.method
			if type(method) ~= "string" or method == "" then
				send_error(id, "bad_request", "missing method")
			elseif type(bridge_dispatcher) ~= "function" then
				send_error(id, "no_bridge", "bridge.lua did not register a dispatcher")
			else
				posix_chdir(GAME_DIR)
				local params = req.params
				if type(params) ~= "table" then params = {} end
				local ok, res = xpcall(function()
					if method == "import_character_json" and import_char_json_fn then
						return import_char_json_fn(params, bridge_dispatcher)
					end
					local out = bridge_dispatcher(method, params)
					if method == "version" and type(out) == "table" and import_char_json_fn then
						if type(out.caps) ~= "table" then out.caps = {} end
						out.caps.importCharacterJson = true
					end
					return out
				end, debug.traceback)
				if ok then
					send_result(id, res)
				else
					send_error(id, "lua_error", tostring(res))
				end
			end
		end
	end
end
