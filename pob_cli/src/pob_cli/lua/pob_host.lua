-- Helpers the native Lua host and its extension modules share: require("pob_host") (the runner
-- puts this directory on package.path), so every module gets the one copy.
local ffi = require("ffi")

local M = {}

function M.norm_path(p)
	if type(p) ~= "string" or p == "" then return "." end
	local s = p:gsub("\\", "/")
	if s == "/" or s:match("^%a:/$") then return s end
	return (s:gsub("/+$", ""))
end

-- PoB's clock (the host's monotonic GetTime), for elapsedMs fields.
function M.now_ms()
	return type(GetTime) == "function" and GetTime() or 0
end

-- A table the JSON writers send as an object even when empty: the host's writer reads __object,
-- dkjson reads __jsontype.
function M.json_obj(t)
	return setmetatable(t or {}, { __object = true, __jsontype = "object" })
end

-- One PoB frame, exactly what its frame loop runs: a pending rebuild (buildFlag) settles inside it
-- the way PoB settles it (global cache wiped, output, sidebar, misc calculator).
function M.frame()
	launch:OnFrame()
end

-- The build mode PoB shows. __mainObject__ is Launch.lua's `launch`; the build lives under
-- launch.main. A pending rebuild is settled first unless opts.rebuild == false (callers that run
-- their own calculation pass); opts.optional returns nil instead of raising when there is none.
function M.get_build(opts)
	local m = type(launch) == "table" and launch.main or nil
	local b = m and m.modes and m.modes["BUILD"]
	if not (b and b.calcsTab) then
		if opts and opts.optional then return nil end
		error("No active PoB build loaded", 0)
	end
	if b.buildFlag and not (opts and opts.rebuild == false) then M.frame() end
	return b
end

-- zlib Inflate / Deflate over a loaded zlib library (libz on POSIX, PoB's zlib1.dll on Windows).
local zlib_declared = false
function M.zlib_codec(lib)
	if not zlib_declared then
		ffi.cdef[[
			typedef struct pob_z_stream_s {
				const unsigned char *next_in; unsigned int avail_in; unsigned long total_in;
				unsigned char *next_out; unsigned int avail_out; unsigned long total_out;
				const char *msg; void *state; void *zalloc; void *zfree; void *opaque;
				int data_type; unsigned long adler; unsigned long reserved;
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
		zlib_declared = true
	end
	local ver, size, chunk = lib.zlibVersion(), ffi.sizeof("pob_z_stream"), 65536
	local Z_OK, Z_STREAM_END, Z_FINISH, Z_NO_FLUSH, Z_BUF_ERROR = 0, 1, 4, 0, -5

	local function inflate_wbits(data, wbits)
		local strm = ffi.new("pob_z_stream")
		if lib.inflateInit2_(strm, wbits, ver, size) ~= Z_OK then return nil, "inflateInit2 failed" end
		strm.next_in, strm.avail_in = ffi.cast("const unsigned char *", data), #data
		local buf, parts = ffi.new("unsigned char[?]", chunk), {}
		while true do
			strm.next_out, strm.avail_out = buf, chunk
			local rc = lib.inflate(strm, Z_NO_FLUSH)
			local produced = chunk - tonumber(strm.avail_out)
			if produced > 0 then parts[#parts + 1] = ffi.string(buf, produced) end
			if rc == Z_STREAM_END then break end
			if rc ~= Z_OK then lib.inflateEnd(strm); return nil, "inflate failed: " .. tostring(rc) end
			if tonumber(strm.avail_in) == 0 and produced == 0 then
				lib.inflateEnd(strm); return nil, "inflate truncated input"
			end
		end
		lib.inflateEnd(strm)
		return table.concat(parts)
	end

	-- zlib or gzip (window bits 47), then raw deflate: the Python guardrail accepts raw share codes.
	local function inflate(data)
		if type(data) ~= "string" then return nil, "Inflate: expected string" end
		if #data == 0 then return "" end
		local out, err = inflate_wbits(data, 47)
		if out ~= nil then return out end
		out = inflate_wbits(data, -15)
		if out ~= nil then return out end
		return nil, err
	end

	local function deflate(data, is_gzip)
		if type(data) ~= "string" then return nil, "Deflate: expected string" end
		local strm = ffi.new("pob_z_stream")
		if lib.deflateInit2_(strm, -1, 8, is_gzip and 31 or 15, 8, 0, ver, size) ~= Z_OK then
			return nil, "deflateInit2 failed"
		end
		strm.next_in, strm.avail_in = ffi.cast("const unsigned char *", data), #data
		local buf, parts = ffi.new("unsigned char[?]", chunk), {}
		while true do
			strm.next_out, strm.avail_out = buf, chunk
			local rc = lib.deflate(strm, Z_FINISH)
			local produced = chunk - tonumber(strm.avail_out)
			if produced > 0 then parts[#parts + 1] = ffi.string(buf, produced) end
			if rc == Z_STREAM_END then break end
			if rc ~= Z_OK and rc ~= Z_BUF_ERROR then lib.deflateEnd(strm); return nil, "deflate failed: " .. tostring(rc) end
		end
		lib.deflateEnd(strm)
		return table.concat(parts)
	end

	return inflate, deflate
end

-- UTF-8 <-> UTF-16 for the Win32 wide-character API (Windows only).
local wide_declared = false
local function declare_wide()
	if wide_declared then return end
	ffi.cdef[[
		int MultiByteToWideChar(unsigned int CodePage, unsigned long dwFlags, const char *lpMultiByteStr,
			int cbMultiByte, uint16_t *lpWideCharStr, int cchWideChar);
		int WideCharToMultiByte(unsigned int CodePage, unsigned long dwFlags, const uint16_t *lpWideCharStr,
			int cchWideChar, char *lpMultiByteStr, int cbMultiByte, const char *lpDefaultChar, int *lpUsedDefaultChar);
	]]
	wide_declared = true
end
local CP_UTF8 = 65001

-- A NUL-terminated UTF-16 copy of `s` (an empty buffer when the conversion fails).
function M.utf8_to_wide(s)
	declare_wide()
	if type(s) ~= "string" then s = tostring(s or "") end
	local n = ffi.C.MultiByteToWideChar(CP_UTF8, 0, s, #s, nil, 0)
	local buf = ffi.new("uint16_t[?]", n + 1)
	if n > 0 then ffi.C.MultiByteToWideChar(CP_UTF8, 0, s, #s, buf, n) end
	buf[n] = 0
	return buf
end

-- UTF-8 text of `wlen` UTF-16 units, or of a NUL-terminated buffer when wlen is omitted.
function M.wide_to_utf8(wbuf, wlen)
	declare_wide()
	wlen = wlen or -1
	local n = ffi.C.WideCharToMultiByte(CP_UTF8, 0, wbuf, wlen, nil, 0, nil, nil)
	if n <= 0 then return "" end
	local buf = ffi.new("char[?]", n + 1)
	ffi.C.WideCharToMultiByte(CP_UTF8, 0, wbuf, wlen, buf, n, nil, nil)
	return ffi.string(buf, wlen == -1 and n - 1 or n)
end

return M
