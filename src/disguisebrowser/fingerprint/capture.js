/* DisguiseBrowser fingerprint capture (docs/FORK_PLAN.md, section 6.1).
 *
 * Plain browser script with no dependencies. It runs in a real browser on a
 * reference machine as well as inside our own browser, so it must not assume
 * anything about the page's environment. Every section is independent: a
 * failure is recorded as { error } and the rest of the capture goes on.
 *
 * Values are grouped the way record.py classifies them: deterministic device
 * and browser facts, persona choices (language, timezone), geometry, noise
 * (render hashes) and environment (things that change between runs).
 */
(() => {
  'use strict';

  const SCHEMA = 1;
  const t0 = performance.now();

  // ------------------------------------------------------------------ helpers
  // 64-bit FNV-1a. BigInt keeps it dependency-free and exact; inputs are small.
  function fnv1a64(bytes) {
    let h = 0xcbf29ce484222325n;
    const prime = 0x100000001b3n;
    const mask = 0xffffffffffffffffn;
    for (let i = 0; i < bytes.length; i++) {
      h ^= BigInt(bytes[i]);
      h = (h * prime) & mask;
    }
    return h.toString(16).padStart(16, '0');
  }
  function hashString(s) {
    const b = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i) & 0xff;
    return fnv1a64(b);
  }
  function hashBuffer(view) {
    return fnv1a64(new Uint8Array(view.buffer, view.byteOffset, view.byteLength));
  }

  async function section(fn) {
    try {
      return await fn();
    } catch (e) {
      return { error: String(e && e.message ? e.message : e) };
    }
  }
  function attempt(fn, fallback = null) {
    try { return fn(); } catch (e) { return fallback; }
  }
  function withTimeout(promise, ms, fallback) {
    return Promise.race([promise, new Promise((r) => setTimeout(() => r(fallback), ms))]);
  }
  function sortedUnique(list) {
    return [...new Set(list)].sort();
  }
  function round(n, digits = 3) {
    return typeof n === 'number' ? Number(n.toFixed(digits)) : n;
  }

  // ------------------------------------------------------------------ scenes
  // Shared with the worker (stringified), so the exact same pixels are drawn.
  function drawTextScene(ctx) {
    ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = '#f60';
    ctx.fillRect(125, 1, 62, 20);
    ctx.fillStyle = '#069';
    ctx.font = '11pt no-real-font-123';
    ctx.fillText('Cwm fjordbank gly \u{1F603}', 2, 15);
    ctx.fillStyle = 'rgba(102, 204, 0, 0.2)';
    ctx.font = '18pt Arial';
    ctx.fillText('Cwm fjordbank gly \u{1F603}', 4, 45);
  }
  function drawGeometryScene(ctx) {
    ctx.globalCompositeOperation = 'multiply';
    for (const [color, x, y] of [['#f2f', 40, 40], ['#2ff', 80, 40], ['#ff2', 60, 80]]) {
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, y, 40, 0, Math.PI * 2, true);
      ctx.closePath();
      ctx.fill();
    }
    ctx.fillStyle = '#f9c';
    ctx.arc(60, 60, 60, 0, Math.PI * 2, true);
    ctx.arc(60, 60, 20, 0, Math.PI * 2, true);
    ctx.fill('evenodd');
  }
  const SCENES = { text: [240, 60, drawTextScene], geometry: [122, 110, drawGeometryScene] };

  const WEBGL_VS = 'attribute vec2 attrVertex;varying vec2 varyinTexCoordinate;uniform vec2 uniformOffset;'
    + 'void main(){varyinTexCoordinate=attrVertex+uniformOffset;gl_Position=vec4(attrVertex,0,1);}';
  const WEBGL_FS = 'precision mediump float;varying vec2 varyinTexCoordinate;'
    + 'void main(){gl_FragColor=vec4(varyinTexCoordinate,0,1);}';
  function drawWebglScene(gl) {
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([-0.2, -0.9, 0, 0.4, -0.26, 0, 0, 0.732134444, 0]), gl.STATIC_DRAW);
    const program = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, WEBGL_VS], [gl.FRAGMENT_SHADER, WEBGL_FS]]) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, src);
      gl.compileShader(shader);
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    gl.useProgram(program);
    const vertex = gl.getAttribLocation(program, 'attrVertex');
    const offset = gl.getUniformLocation(program, 'uniformOffset');
    gl.enableVertexAttribArray(vertex);
    gl.vertexAttribPointer(vertex, 3, gl.FLOAT, false, 0, 0);
    gl.uniform2f(offset, 1, 1);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 3);
    const pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
    gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
    return pixels;
  }

  // ------------------------------------------------------------------ sections
  function navigatorInfo() {
    const n = navigator;
    return {
      userAgent: n.userAgent, appVersion: n.appVersion, appName: n.appName,
      appCodeName: n.appCodeName, product: n.product, productSub: n.productSub,
      vendor: n.vendor, vendorSub: n.vendorSub, platform: n.platform, oscpu: n.oscpu ?? null,
      language: n.language, languages: [...n.languages],
      hardwareConcurrency: n.hardwareConcurrency, deviceMemory: n.deviceMemory ?? null,
      maxTouchPoints: n.maxTouchPoints, pdfViewerEnabled: n.pdfViewerEnabled ?? null,
      webdriver: n.webdriver, cookieEnabled: n.cookieEnabled, doNotTrack: n.doNotTrack,
      globalPrivacyControl: n.globalPrivacyControl ?? null, onLine: n.onLine,
      plugins: [...n.plugins].map((p) => ({
        name: p.name, filename: p.filename, description: p.description,
        mimeTypes: [...p].map((m) => m.type),
      })),
      mimeTypes: [...n.mimeTypes].map((m) => ({ type: m.type, suffixes: m.suffixes })),
      ownProperties: Object.getOwnPropertyNames(n),
    };
  }

  async function uaDataInfo() {
    const d = navigator.userAgentData;
    if (!d) return { available: false, secureContext: isSecureContext };
    const high = await d.getHighEntropyValues([
      'architecture', 'bitness', 'model', 'platformVersion', 'uaFullVersion',
      'fullVersionList', 'wow64', 'formFactors',
    ]);
    return { available: true, brands: d.brands, mobile: d.mobile, platform: d.platform, ...high };
  }

  const DROP_HEADERS = new Set(['host', 'referer', 'cookie', 'connection', 'content-length',
    'content-type', 'origin', 'if-none-match', 'if-modified-since']);
  function normalizeHeaders(raw) {
    const entries = (raw || []).map(([k, v]) => [k.toLowerCase(), v]);
    const kept = entries.filter(([k]) => !DROP_HEADERS.has(k));
    const values = {};
    for (const [k, v] of kept) values[k] = k in values ? `${values[k]}, ${v}` : v;
    return { values, order: entries.map(([k]) => k) };
  }
  async function headersInfo() {
    const embedded = attempt(() => JSON.parse(document.getElementById('nav-headers').textContent), null);
    const out = { navigation: embedded ? normalizeHeaders(embedded) : null };
    const res = await fetch('echo', { cache: 'no-store' });
    out.fetch = normalizeHeaders(await res.json());
    return out;
  }

  function screenInfo() {
    const s = screen;
    return {
      width: s.width, height: s.height, availWidth: s.availWidth, availHeight: s.availHeight,
      availLeft: s.availLeft ?? null, availTop: s.availTop ?? null,
      colorDepth: s.colorDepth, pixelDepth: s.pixelDepth, devicePixelRatio: window.devicePixelRatio,
      isExtended: s.isExtended ?? null,
      orientation: s.orientation ? { type: s.orientation.type, angle: s.orientation.angle } : null,
      window: {
        outerWidth, outerHeight, innerWidth, innerHeight,
        chromeHeight: outerHeight - innerHeight, chromeWidth: outerWidth - innerWidth,
        screenX: window.screenX, screenY: window.screenY,
        visualViewportScale: window.visualViewport ? window.visualViewport.scale : null,
      },
    };
  }

  const MEDIA_QUERIES = [
    '(prefers-color-scheme: dark)', '(prefers-color-scheme: light)',
    '(prefers-reduced-motion: reduce)', '(prefers-reduced-transparency: reduce)',
    '(prefers-contrast: more)', '(prefers-contrast: less)', '(forced-colors: active)',
    '(inverted-colors: inverted)', '(color-gamut: srgb)', '(color-gamut: p3)',
    '(color-gamut: rec2020)', '(dynamic-range: high)', '(video-dynamic-range: high)',
    '(pointer: fine)', '(pointer: coarse)', '(pointer: none)', '(hover: hover)',
    '(any-pointer: fine)', '(any-pointer: coarse)', '(any-hover: hover)', '(update: fast)',
    '(overflow-block: scroll)', '(overflow-inline: scroll)', '(display-mode: browser)',
    '(scripting: enabled)', '(monochrome)', '(scan: progressive)', '(grid: 0)',
    '(orientation: landscape)', '(min-resolution: 2dppx)', '(min-resolution: 1.5dppx)',
    '(min-resolution: 1.25dppx)', '(color: 8)', '(color: 10)',
  ];
  function mediaQueriesInfo() {
    const out = {};
    for (const q of MEDIA_QUERIES) out[q] = matchMedia(q).matches;
    return out;
  }

  function intlInfo() {
    const dtf = new Intl.DateTimeFormat().resolvedOptions();
    const date = new Date(2024, 0, 15, 13, 45, 30);
    return {
      dateTimeFormat: dtf,
      numberFormat: new Intl.NumberFormat().resolvedOptions(),
      collator: new Intl.Collator().resolvedOptions(),
      pluralRules: new Intl.PluralRules().resolvedOptions(),
      relativeTimeFormat: attempt(() => new Intl.RelativeTimeFormat().resolvedOptions()),
      timezoneOffsetMinutes: new Date().getTimezoneOffset(),
      dateString: date.toString(), dateLocaleString: date.toLocaleString(),
      numberLocaleString: (1234567.891).toLocaleString(),
      supportedTimeZones: attempt(() => Intl.supportedValuesOf('timeZone').length),
      supportedCalendars: attempt(() => Intl.supportedValuesOf('calendar')),
      supportedCurrencies: attempt(() => Intl.supportedValuesOf('currency').length),
      supportedUnits: attempt(() => Intl.supportedValuesOf('unit')),
    };
  }

  const INTERFACES = [
    'Navigator', 'Screen', 'Document', 'HTMLDocument', 'Element', 'HTMLElement', 'Node', 'Event',
    'MouseEvent', 'PointerEvent', 'KeyboardEvent', 'WheelEvent', 'TouchEvent', 'Touch',
    'HTMLCanvasElement', 'CanvasRenderingContext2D', 'OffscreenCanvas',
    'OffscreenCanvasRenderingContext2D', 'ImageBitmap', 'WebGLRenderingContext',
    'WebGL2RenderingContext', 'GPU', 'GPUAdapter', 'GPUAdapterInfo', 'GPUSupportedLimits',
    'HTMLMediaElement', 'HTMLVideoElement', 'MediaDevices', 'MediaCapabilities', 'MediaSource',
    'MediaKeys', 'MediaSession', 'Performance', 'PerformanceNavigationTiming', 'Permissions',
    'PermissionStatus', 'Notification', 'AudioContext', 'BaseAudioContext', 'OfflineAudioContext',
    'AudioBuffer', 'AnalyserNode', 'AudioWorklet', 'RTCPeerConnection', 'RTCRtpSender',
    'RTCRtpReceiver', 'Bluetooth', 'USB', 'HID', 'Serial', 'Keyboard', 'NavigatorUAData',
    'SpeechSynthesis', 'SpeechSynthesisVoice', 'SpeechRecognition', 'webkitSpeechRecognition',
    'Credential', 'PublicKeyCredential', 'CredentialsContainer', 'FontFaceSet', 'FontData',
    'Clipboard', 'StorageManager', 'Scheduler', 'Worker', 'SharedWorker', 'ServiceWorkerContainer',
    'XRSystem', 'Gamepad', 'Geolocation', 'BatteryManager', 'NetworkInformation', 'WakeLock',
    'IdleDetector', 'PaymentRequest', 'Presentation', 'ContactsManager', 'EyeDropper',
    'BarcodeDetector', 'Ink', 'WindowControlsOverlay', 'VirtualKeyboard', 'LaunchQueue',
    'CookieStore', 'TrustedTypePolicyFactory', 'Highlight', 'ViewTransition', 'Sanitizer',
    'CSSStyleDeclaration', 'CSS', 'Intl', 'Math', 'JSON', 'Reflect', 'Atomics', 'WebAssembly',
    'Temporal', 'Iterator', 'Promise', 'Array', 'String', 'Object', 'Error', 'Date', 'RegExp',
    'Function', 'Symbol', 'Map', 'Set', 'WeakRef', 'FinalizationRegistry', 'SharedArrayBuffer',
    'ArrayBuffer', 'DataView', 'Float16Array', 'BigInt',
  ];
  const CONSTANT = /^[A-Z][A-Z0-9_]*$/;
  function names(obj, dropConstants) {
    let list = Object.getOwnPropertyNames(obj);
    if (dropConstants) list = list.filter((n) => !CONSTANT.test(n));
    return list.sort();
  }
  function cleanRealm() {
    const iframe = document.createElement('iframe');
    iframe.style.cssText = 'position:absolute;left:-10000px;top:0;width:300px;height:150px';
    iframe.setAttribute('aria-hidden', 'true');
    document.body.appendChild(iframe);
    return iframe;
  }
  function apiSurfaceInfo(w) {
    const out = { globals: names(w), interfaces: {} };
    for (const name of INTERFACES) {
      const target = w[name];
      if (target === undefined || target === null) { out.interfaces[name] = null; continue; }
      const dropConstants = /WebGL|GL/.test(name);
      const entry = {};
      if (typeof target === 'function') {
        entry.static = names(target, dropConstants);
        if (target.prototype) entry.prototype = names(target.prototype, dropConstants);
      } else {
        entry.static = names(target, dropConstants);
        if (Object.getPrototypeOf(target) && Object.getPrototypeOf(target) !== w.Object.prototype) {
          entry.prototype = names(Object.getPrototypeOf(target), dropConstants);
        }
      }
      out.interfaces[name] = entry;
    }
    return out;
  }

  function jsEngineInfo(w) {
    const stack = attempt(() => new w.Error('probe').stack, '') || '';
    const lines = stack.split('\n');
    return {
      errorStackFirstLine: lines[0] || null,
      errorStackFrameShape: (lines[1] || '').replace(/\d+/g, 'N').replace(/https?:\/\/\S+/g, 'URL').trim() || null,
      stackTraceLimit: w.Error.stackTraceLimit,
      captureStackTrace: typeof w.Error.captureStackTrace,
      nativeToString: Function.prototype.toString.call(w.Math.max),
      arrowToString: (() => 1).toString(),
      bigIntSupport: typeof w.BigInt, weakRef: typeof w.WeakRef, temporal: typeof w.Temporal,
      sharedArrayBuffer: typeof w.SharedArrayBuffer, crossOriginIsolated: w.crossOriginIsolated,
      wasm: {
        present: typeof w.WebAssembly,
        simd: attempt(() => w.WebAssembly.validate(new Uint8Array([
          0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0,
          253, 15, 253, 98, 11]))),
        threads: typeof w.SharedArrayBuffer !== 'undefined',
      },
      mathFingerprint: hashString([
        Math.tan(-1e300), Math.sin(1e300), Math.cos(1e300), Math.exp(1), Math.log1p(10),
        Math.expm1(1), Math.cosh(1), Math.sinh(1), Math.tanh(1), Math.asinh(1), Math.acosh(1e300),
        Math.atanh(0.5), Math.cbrt(100), Math.pow(Math.PI, -100), Math.atan2(1e-310, 2),
      ].map((v) => v.toString()).join('|')),
      dateParse: Date.parse('2024-01-15T13:45:30'),
      toLocaleStringDefault: (0.1 + 0.2).toString(),
    };
  }

  const WEBGL1_PARAMS = [
    'ALIASED_LINE_WIDTH_RANGE', 'ALIASED_POINT_SIZE_RANGE', 'ALPHA_BITS', 'BLUE_BITS', 'DEPTH_BITS',
    'GREEN_BITS', 'RED_BITS', 'STENCIL_BITS', 'SUBPIXEL_BITS', 'SAMPLE_BUFFERS', 'SAMPLES',
    'MAX_COMBINED_TEXTURE_IMAGE_UNITS', 'MAX_CUBE_MAP_TEXTURE_SIZE', 'MAX_FRAGMENT_UNIFORM_VECTORS',
    'MAX_RENDERBUFFER_SIZE', 'MAX_TEXTURE_IMAGE_UNITS', 'MAX_TEXTURE_SIZE', 'MAX_VARYING_VECTORS',
    'MAX_VERTEX_ATTRIBS', 'MAX_VERTEX_TEXTURE_IMAGE_UNITS', 'MAX_VERTEX_UNIFORM_VECTORS',
    'MAX_VIEWPORT_DIMS', 'RENDERER', 'SHADING_LANGUAGE_VERSION', 'VENDOR', 'VERSION',
    'IMPLEMENTATION_COLOR_READ_FORMAT', 'IMPLEMENTATION_COLOR_READ_TYPE',
  ];
  const WEBGL2_PARAMS = [
    'MAX_3D_TEXTURE_SIZE', 'MAX_ARRAY_TEXTURE_LAYERS', 'MAX_CLIENT_WAIT_TIMEOUT_WEBGL',
    'MAX_COLOR_ATTACHMENTS', 'MAX_COMBINED_FRAGMENT_UNIFORM_COMPONENTS', 'MAX_COMBINED_UNIFORM_BLOCKS',
    'MAX_COMBINED_VERTEX_UNIFORM_COMPONENTS', 'MAX_DRAW_BUFFERS', 'MAX_ELEMENT_INDEX',
    'MAX_ELEMENTS_INDICES', 'MAX_ELEMENTS_VERTICES', 'MAX_FRAGMENT_INPUT_COMPONENTS',
    'MAX_FRAGMENT_UNIFORM_BLOCKS', 'MAX_FRAGMENT_UNIFORM_COMPONENTS', 'MAX_PROGRAM_TEXEL_OFFSET',
    'MAX_SAMPLES', 'MAX_SERVER_WAIT_TIMEOUT', 'MAX_TEXTURE_LOD_BIAS',
    'MAX_TRANSFORM_FEEDBACK_INTERLEAVED_COMPONENTS', 'MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS',
    'MAX_TRANSFORM_FEEDBACK_SEPARATE_COMPONENTS', 'MAX_UNIFORM_BLOCK_SIZE', 'MAX_UNIFORM_BUFFER_BINDINGS',
    'MAX_VARYING_COMPONENTS', 'MAX_VERTEX_OUTPUT_COMPONENTS', 'MAX_VERTEX_UNIFORM_BLOCKS',
    'MAX_VERTEX_UNIFORM_COMPONENTS', 'MIN_PROGRAM_TEXEL_OFFSET', 'UNIFORM_BUFFER_OFFSET_ALIGNMENT',
  ];
  function glValue(v) {
    if (v === null || v === undefined) return null;
    if (ArrayBuffer.isView(v)) return Array.from(v);
    return v;
  }
  function webglInfo(kind) {
    const canvas = document.createElement('canvas');
    canvas.width = 256; canvas.height = 128;
    const gl = canvas.getContext(kind, { preserveDrawingBuffer: true });
    if (!gl) return { available: false };
    const out = { available: true, contextAttributes: gl.getContextAttributes() };
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    out.unmaskedVendor = debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : null;
    out.unmaskedRenderer = debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null;
    out.parameters = {};
    const params = kind === 'webgl2' ? WEBGL1_PARAMS.concat(WEBGL2_PARAMS) : WEBGL1_PARAMS;
    for (const name of params) out.parameters[name] = glValue(gl.getParameter(gl[name]));
    out.compressedTextureFormats = glValue(gl.getParameter(gl.COMPRESSED_TEXTURE_FORMATS));
    out.extensions = (gl.getSupportedExtensions() || []).slice().sort();
    const aniso = gl.getExtension('EXT_texture_filter_anisotropic')
      || gl.getExtension('WEBKIT_EXT_texture_filter_anisotropic');
    out.maxAnisotropy = aniso ? gl.getParameter(aniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT) : null;
    if (kind === 'webgl') {
      const db = gl.getExtension('WEBGL_draw_buffers');
      out.drawBuffers = db ? {
        maxColorAttachments: gl.getParameter(db.MAX_COLOR_ATTACHMENTS_WEBGL),
        maxDrawBuffers: gl.getParameter(db.MAX_DRAW_BUFFERS_WEBGL),
      } : null;
    }
    out.precision = {};
    for (const shader of ['VERTEX_SHADER', 'FRAGMENT_SHADER']) {
      for (const p of ['LOW_FLOAT', 'MEDIUM_FLOAT', 'HIGH_FLOAT', 'LOW_INT', 'MEDIUM_INT', 'HIGH_INT']) {
        const f = gl.getShaderPrecisionFormat(gl[shader], gl[p]);
        out.precision[`${shader}.${p}`] = f ? [f.rangeMin, f.rangeMax, f.precision] : null;
      }
    }
    out.drawingBufferColorSpace = gl.drawingBufferColorSpace ?? null;
    out.unpackColorSpace = gl.unpackColorSpace ?? null;
    out.debugShaders = gl.getExtension('WEBGL_debug_shaders') !== null;
    const pixels = drawWebglScene(gl);
    out.render = {
      pixelsHash: hashBuffer(pixels),
      dataUrlHash: hashString(canvas.toDataURL()),
    };
    const again = drawWebglScene(gl);
    out.renderStable = hashBuffer(again) === out.render.pixelsHash;
    out.errorAfterRender = gl.getError();
    attempt(() => gl.getExtension('WEBGL_lose_context').loseContext());
    return out;
  }

  async function webgpuInfo() {
    if (!navigator.gpu) return { available: false, secureContext: isSecureContext };
    const out = {
      available: true,
      preferredCanvasFormat: attempt(() => navigator.gpu.getPreferredCanvasFormat()),
      wgslLanguageFeatures: attempt(() => [...navigator.gpu.wgslLanguageFeatures].sort()),
      adapters: {},
    };
    for (const pref of ['default', 'high-performance', 'low-power']) {
      const options = pref === 'default' ? {} : { powerPreference: pref };
      const adapter = await withTimeout(navigator.gpu.requestAdapter(options), 5000, undefined);
      if (adapter === undefined) { out.adapters[pref] = { error: 'timeout' }; continue; }
      if (!adapter) { out.adapters[pref] = null; continue; }
      const info = adapter.info || (adapter.requestAdapterInfo ? await adapter.requestAdapterInfo() : null);
      const limits = {};
      for (const k of Object.getOwnPropertyNames(Object.getPrototypeOf(adapter.limits))) {
        if (k === 'constructor') continue;
        const v = attempt(() => adapter.limits[k]);
        if (typeof v !== 'function' && v !== null) limits[k] = v;
      }
      out.adapters[pref] = {
        info: info ? {
          vendor: info.vendor, architecture: info.architecture, device: info.device,
          description: info.description, subgroupMinSize: info.subgroupMinSize ?? null,
          subgroupMaxSize: info.subgroupMaxSize ?? null,
          isFallbackAdapter: info.isFallbackAdapter ?? null,
        } : null,
        isFallbackAdapter: adapter.isFallbackAdapter ?? null,
        features: [...adapter.features].sort(),
        limits,
      };
    }
    return out;
  }

  function sceneHashes(width, height, draw, CanvasCtor) {
    const canvas = CanvasCtor ? new CanvasCtor(width, height) : document.createElement('canvas');
    if (!CanvasCtor) { canvas.width = width; canvas.height = height; }
    const ctx = canvas.getContext('2d');
    draw(ctx);
    const data = ctx.getImageData(0, 0, width, height).data;
    const out = { pixelsHash: hashBuffer(data) };
    if (!CanvasCtor) {
      const url = canvas.toDataURL();
      out.dataUrlHash = hashString(url);
      out.dataUrlLength = url.length;
    }
    return out;
  }
  function canvasInfo() {
    const out = { scenes: {} };
    for (const [name, [w, h, draw]] of Object.entries(SCENES)) {
      const first = sceneHashes(w, h, draw);
      const second = sceneHashes(w, h, draw);
      out.scenes[name] = { ...first, stable: first.pixelsHash === second.pixelsHash };
      if (typeof OffscreenCanvas === 'function') {
        out.scenes[name].offscreenPixelsHash = sceneHashes(w, h, draw, OffscreenCanvas).pixelsHash;
      }
    }
    const blank = document.createElement('canvas');
    blank.width = 100; blank.height = 100;
    const bctx = blank.getContext('2d');
    bctx.fillStyle = '#fff';
    bctx.fillRect(0, 0, 100, 100);
    const white = bctx.getImageData(0, 0, 100, 100).data;
    out.white = { pure: white.every((v) => v === 255), pixelsHash: hashBuffer(white) };
    const wctx = document.createElement('canvas').getContext('2d');
    wctx.rect(0, 0, 10, 10); wctx.rect(2, 2, 6, 6);
    out.windingEvenOdd = wctx.isPointInPath(5, 5, 'evenodd') === false;
    out.textRenderingSupport = typeof wctx.letterSpacing === 'string';
    out.filterSupport = typeof wctx.filter === 'string';
    out.roundRect = typeof wctx.roundRect === 'function';
    return out;
  }

  const GENERICS = new Set(['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui',
    'ui-serif', 'ui-sans-serif', 'ui-monospace', 'ui-rounded', 'emoji', 'math']);
  function cssFont(size, family) {
    return `${size}px ${GENERICS.has(family) ? family : `"${family}"`}`;
  }
  const METRIC_FONTS = [
    'Arial', 'Times New Roman', 'Courier New', 'Verdana', 'Tahoma', 'Georgia', 'Segoe UI', 'Calibri',
    'Cambria', 'Consolas', 'Helvetica', 'Helvetica Neue', 'Menlo', 'Monaco', 'Lucida Grande',
    'DejaVu Sans', 'Liberation Sans', 'Noto Sans', 'Ubuntu', 'Roboto', 'system-ui', 'sans-serif',
    'serif', 'monospace', 'cursive', 'fantasy', 'ui-monospace', 'ui-sans-serif', 'ui-serif',
  ];
  const METRIC_STRINGS = [
    'mmmmmmmmmmlli',
    'The quick brown fox jumps over the lazy dog 0123456789',
    'WwMmIiLl|!@#$%^&*()_+ \u00c4\u00d6\u00dc \u042f\u0439 \u4e2d\u6587',
  ];
  function textMetricsInfo() {
    const ctx = document.createElement('canvas').getContext('2d');
    const span = document.createElement('span');
    span.style.cssText = 'position:absolute;left:-10000px;top:0;white-space:nowrap;font-size:16px;line-height:normal';
    document.body.appendChild(span);
    const out = {};
    for (const font of METRIC_FONTS) {
      const entry = {};
      ctx.font = cssFont(16, font);
      span.style.fontFamily = GENERICS.has(font) ? font : `"${font}"`;
      METRIC_STRINGS.forEach((text, i) => {
        const m = ctx.measureText(text);
        span.textContent = text;
        const rect = span.getBoundingClientRect();
        entry[`s${i}`] = {
          width: round(m.width, 4),
          ascent: round(m.actualBoundingBoxAscent, 4), descent: round(m.actualBoundingBoxDescent, 4),
          left: round(m.actualBoundingBoxLeft, 4), right: round(m.actualBoundingBoxRight, 4),
          fontAscent: round(m.fontBoundingBoxAscent, 4), fontDescent: round(m.fontBoundingBoxDescent, 4),
          domWidth: round(rect.width, 4), domHeight: round(rect.height, 4),
        };
      });
      out[font] = entry;
    }
    span.remove();
    return out;
  }

  const EMOJI = ['\u{1F603}', '\u{1F602}', '\u{1F923}', '\u2764\uFE0F', '\u{1F44D}\u{1F3FD}',
    '\u{1F1FA}\u{1F1F8}', '\u{1F468}\u200D\u{1F469}\u200D\u{1F467}\u200D\u{1F466}', '\u{1F9D1}\u200D\u{1F4BB}',
    '\u{1FAE0}', '\u{1FAF6}', '\u{1F600}', '\u{1F4A9}', '\u{1F680}', '\u{1F30D}', '\u{1F3F3}\uFE0F\u200D\u{1F308}',
    '\u{1F469}\u200D\u{1F680}', '\u00a9\uFE0F', '\u2122\uFE0F', '\u{1F9E0}', '\u{1FAE8}'];
  function emojiInfo() {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = '16px sans-serif';
    const span = document.createElement('span');
    span.style.cssText = 'position:absolute;left:-10000px;top:0;white-space:nowrap;font:16px sans-serif;line-height:normal';
    document.body.appendChild(span);
    const out = {};
    for (const e of EMOJI) {
      span.textContent = e;
      const r = span.getBoundingClientRect();
      out[e] = { canvasWidth: round(ctx.measureText(e).width, 4), domWidth: round(r.width, 4), domHeight: round(r.height, 4) };
    }
    span.remove();
    return out;
  }

  const FONT_LIST = [
    // Windows
    'Arial', 'Arial Black', 'Arial Narrow', 'Arial Unicode MS', 'Bahnschrift', 'Calibri', 'Calibri Light',
    'Cambria', 'Cambria Math', 'Candara', 'Comic Sans MS', 'Consolas', 'Constantia', 'Corbel',
    'Courier New', 'Ebrima', 'Franklin Gothic Medium', 'Gabriola', 'Gadugi', 'Georgia',
    'HoloLens MDL2 Assets', 'Impact', 'Ink Free', 'Javanese Text', 'Leelawadee UI', 'Lucida Console',
    'Lucida Sans Unicode', 'Malgun Gothic', 'Marlett', 'Microsoft Himalaya', 'Microsoft JhengHei',
    'Microsoft New Tai Lue', 'Microsoft PhagsPa', 'Microsoft Sans Serif', 'Microsoft Tai Le',
    'Microsoft YaHei', 'Microsoft Yi Baiti', 'MingLiU-ExtB', 'Mongolian Baiti', 'MS Gothic',
    'MS UI Gothic', 'MS PGothic', 'MV Boli', 'Myanmar Text', 'Nirmala UI', 'Palatino Linotype',
    'Segoe Fluent Icons', 'Segoe MDL2 Assets', 'Segoe Print', 'Segoe Script', 'Segoe UI',
    'Segoe UI Emoji', 'Segoe UI Historic', 'Segoe UI Symbol', 'Segoe UI Variable', 'SimSun',
    'SimSun-ExtB', 'Sitka', 'Sylfaen', 'Symbol', 'Tahoma', 'Times New Roman', 'Trebuchet MS',
    'Verdana', 'Webdings', 'Wingdings', 'Yu Gothic', 'Yu Gothic UI', 'Cascadia Code', 'Cascadia Mono',
    // Microsoft Office / legacy
    'Book Antiqua', 'Bookman Old Style', 'Century Gothic', 'Century Schoolbook', 'Garamond',
    'Rockwell', 'Tw Cen MT', 'Perpetua', 'Gill Sans MT', 'Lucida Bright', 'Lucida Handwriting',
    'Lucida Sans', 'Mistral', 'Papyrus', 'Algerian', 'Bodoni MT', 'Broadway', 'Castellar',
    'Copperplate Gothic Bold', 'Elephant', 'Harrington', 'Niagara Solid', 'Vivaldi', 'Wide Latin',
    'Calisto MT', 'Baskerville Old Face', 'Bell MT', 'Agency FB', 'Berlin Sans FB',
    'Bernard MT Condensed', 'Blackadder ITC', 'Bradley Hand ITC', 'Brush Script MT', 'Centaur',
    'Chiller', 'Colonna MT', 'Cooper Black', 'Curlz MT', 'Edwardian Script ITC', 'Engravers MT',
    'Eras Medium ITC', 'Felix Titling', 'Footlight MT Light', 'Forte', 'Freestyle Script',
    'French Script MT', 'Gigi', 'Goudy Old Style', 'Goudy Stout', 'Haettenschweiler',
    'High Tower Text', 'Imprint MT Shadow', 'Informal Roman', 'Jokerman', 'Juice ITC', 'Kristen ITC',
    'Kunstler Script', 'Magneto', 'Maiandra GD', 'Matura MT Script Capitals', 'Modern No. 20',
    'Monotype Corsiva', 'MS Reference Sans Serif', 'MS Reference Specialty', 'OCR A Extended',
    'Old English Text MT', 'Onyx', 'Palace Script MT', 'Parchment', 'Playbill', 'Poor Richard',
    'Pristina', 'Rage Italic', 'Ravie', 'Script MT Bold', 'Showcard Gothic', 'Snap ITC', 'Stencil',
    'Tempus Sans ITC', 'Viner Hand ITC', 'Vladimir Script', 'Aptos', 'Grandview', 'Seaford',
    'Skeena', 'Tenorite', 'Bierstadt',
    // macOS
    'Helvetica', 'Helvetica Neue', 'Geneva', 'Monaco', 'Menlo', 'Lucida Grande', 'American Typewriter',
    'Avenir', 'Avenir Next', 'Baskerville', 'Chalkboard', 'Chalkboard SE', 'Charter', 'Cochin',
    'Copperplate', 'Didot', 'Futura', 'Gill Sans', 'Hoefler Text', 'Marker Felt', 'Noteworthy',
    'Optima', 'Palatino', 'PingFang SC', 'Hiragino Sans', 'Hiragino Kaku Gothic ProN', 'Apple Chancery',
    'Apple Color Emoji', 'Apple SD Gothic Neo', 'Apple Symbols', 'Arial Hebrew', 'Arial Rounded MT Bold',
    'Big Caslon', 'Bodoni 72', 'Bradley Hand', 'Herculanum', 'Kefa', 'Luminari', 'Phosphate',
    'Savoye LET', 'SignPainter', 'Skia', 'Snell Roundhand', 'Trattatello', 'Zapfino', 'SF Pro',
    'SF Mono', 'SF Compact', 'New York', 'Athelas', 'Seravek', 'Superclarendon', 'Iowan Old Style',
    // Linux
    'DejaVu Sans', 'DejaVu Serif', 'DejaVu Sans Mono', 'Liberation Sans', 'Liberation Serif',
    'Liberation Mono', 'Liberation Sans Narrow', 'Noto Sans', 'Noto Serif', 'Noto Sans Mono',
    'Noto Color Emoji', 'Noto Sans CJK JP', 'Noto Sans CJK SC', 'Noto Sans Symbols',
    'Noto Sans Symbols2', 'Noto Emoji', 'Ubuntu', 'Ubuntu Mono', 'Ubuntu Sans', 'Cantarell',
    'Droid Sans', 'Droid Sans Mono', 'FreeSans', 'FreeSerif', 'FreeMono', 'Nimbus Sans',
    'Nimbus Roman', 'Nimbus Mono PS', 'URW Gothic', 'URW Bookman', 'C059', 'P052', 'D050000L',
    'Z003', 'Bitstream Vera Sans', 'Bitstream Charter', 'Open Sans', 'Roboto', 'Roboto Mono',
    'Source Sans Pro', 'Source Sans 3', 'Source Code Pro', 'Fira Sans', 'Fira Code', 'Fira Mono',
    'Lato', 'Carlito', 'Caladea', 'Selawik', 'Gelasio', 'Inter', 'Hack', 'Inconsolata', 'Terminus',
    'Cousine', 'Tinos', 'Arimo', 'Lohit Devanagari', 'Padauk', 'Garuda', 'Kinnari', 'Norasi',
    'Purisa', 'Sawasdee', 'Tlwg Typo', 'Umpush', 'Waree', 'Mukti Narrow', 'Rachana', 'Meera',
    'Gargi', 'Khmer OS', 'Abyssinica SIL', 'Symbola', 'Twemoji Mozilla', 'EmojiOne Color',
    'JoyPixels', 'Cantarell Extra Bold', 'Oxygen', 'Oxygen Mono', 'KacstOne', 'Loma', 'Mitra Mono',
    'Nakula', 'Sahadeva', 'Samanata', 'Sarai', 'Tibetan Machine Uni', 'Vemana2000', 'Yrsa',
    'Noto Sans Arabic', 'Noto Naskh Arabic', 'Noto Sans Hebrew', 'Noto Sans Thai', 'Noto Sans Devanagari',
    'PT Sans', 'PT Serif', 'PT Mono', 'Ubuntu Condensed', 'Comfortaa', 'Monospace', 'Courier',
    'Times', 'Helvetica Inserat', 'Wine Tahoma',
  ];
  function fontsInfo() {
    const ctx = document.createElement('canvas').getContext('2d');
    const bases = ['monospace', 'sans-serif', 'serif'];
    const probes = ['mmmmmmmmmmlli', 'WwMmIiLl0O'];
    const measure = (family) => {
      ctx.font = cssFont(72, family);
      return probes.map((p) => ctx.measureText(p).width);
    };
    const baseWidths = {};
    for (const b of bases) baseWidths[b] = measure(b);
    const detected = [];
    for (const font of FONT_LIST) {
      let differs = false;
      for (const b of bases) {
        ctx.font = `72px "${font}", ${b}`;
        const widths = probes.map((p) => ctx.measureText(p).width);
        if (widths.some((w, i) => w !== baseWidths[b][i])) { differs = true; break; }
      }
      if (differs) detected.push(font);
    }
    const resolvedGenerics = {};
    for (const g of [...bases, 'system-ui', 'cursive', 'fantasy', 'ui-monospace', 'ui-sans-serif', 'ui-serif']) {
      const target = measure(g);
      resolvedGenerics[g] = detected.filter((f) => {
        const w = measure(f);
        return w.every((v, i) => v === target[i]);
      });
    }
    return { tested: FONT_LIST.length, detected: detected.sort(), resolvedGenerics, local: null };
  }

  const CONTROLS = [
    ['button', 'Button'], ['input-text', null], ['input-checkbox', null], ['input-radio', null],
    ['input-range', null], ['input-date', null], ['input-color', null], ['input-file', null],
    ['input-number', null], ['input-search', null], ['select', 'Option'], ['textarea', null],
    ['meter', null], ['progress', null],
  ];
  function formControlsInfo(w) {
    const doc = w.document;
    doc.body.style.margin = '0';
    const out = {};
    for (const [kind, text] of CONTROLS) {
      let el;
      if (kind.startsWith('input-')) {
        el = doc.createElement('input');
        el.type = kind.slice(6);
      } else {
        el = doc.createElement(kind);
        if (kind === 'select') { const o = doc.createElement('option'); o.textContent = text; el.appendChild(o); }
        else if (text) el.textContent = text;
      }
      doc.body.appendChild(el);
      const r = el.getBoundingClientRect();
      const cs = w.getComputedStyle(el);
      out[kind] = {
        width: round(r.width, 3), height: round(r.height, 3),
        fontFamily: cs.fontFamily, fontSize: cs.fontSize, lineHeight: cs.lineHeight,
        padding: cs.padding, border: cs.borderTopWidth, color: cs.color, background: cs.backgroundColor,
      };
      el.remove();
    }
    const scroller = doc.createElement('div');
    scroller.style.cssText = 'width:100px;height:100px;overflow:scroll';
    doc.body.appendChild(scroller);
    out.scrollbarWidth = scroller.offsetWidth - scroller.clientWidth;
    out.scrollbarHeight = scroller.offsetHeight - scroller.clientHeight;
    scroller.remove();
    return out;
  }

  function defaultFontsInfo(w) {
    const doc = w.document;
    const out = {};
    for (const tag of ['body', 'p', 'pre', 'code', 'tt', 'h1', 'button', 'input', 'select', 'textarea']) {
      const el = tag === 'body' ? doc.body : doc.createElement(tag);
      if (tag !== 'body') doc.body.appendChild(el);
      const cs = w.getComputedStyle(el);
      out[tag] = { fontFamily: cs.fontFamily, fontSize: cs.fontSize, fontWeight: cs.fontWeight };
      if (tag !== 'body') el.remove();
    }
    return out;
  }

  async function audioInfo() {
    const out = {};
    out.context = await section(async () => {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctor();
      const info = {
        sampleRate: ctx.sampleRate, baseLatency: ctx.baseLatency ?? null,
        outputLatency: ctx.outputLatency ?? null, state: ctx.state,
        maxChannelCount: ctx.destination.maxChannelCount, channelCount: ctx.destination.channelCount,
        channelCountMode: ctx.destination.channelCountMode,
        channelInterpretation: ctx.destination.channelInterpretation,
        sinkId: typeof ctx.sinkId === 'string' ? ctx.sinkId : (ctx.sinkId ? 'object' : null),
      };
      await ctx.close();
      return info;
    });
    const render = async () => {
      const oc = new OfflineAudioContext(1, 44100, 44100);
      const osc = oc.createOscillator();
      osc.type = 'triangle';
      osc.frequency.value = 10000;
      const comp = oc.createDynamicsCompressor();
      comp.threshold.value = -50; comp.knee.value = 40; comp.ratio.value = 12;
      comp.attack.value = 0; comp.release.value = 0.25;
      osc.connect(comp);
      comp.connect(oc.destination);
      osc.start(0);
      const buf = await oc.startRendering();
      return buf.getChannelData(0);
    };
    out.offline = await section(async () => {
      const a = await render();
      const b = await render();
      let sum = 0;
      for (let i = 4500; i < 5000; i++) sum += Math.abs(a[i]);
      return { hash: hashBuffer(a), sum: sum.toString(), stable: hashBuffer(b) === hashBuffer(a), length: a.length };
    });
    return out;
  }

  const MEDIA_TYPES = [
    'video/mp4; codecs="avc1.42E01E"', 'video/mp4; codecs="avc1.640028"', 'video/mp4; codecs="avc1.6E0033"',
    'video/mp4; codecs="hvc1.1.6.L93.B0"', 'video/mp4; codecs="hev1.1.6.L93.B0"',
    'video/mp4; codecs="hvc1.2.4.L120.B0"', 'video/mp4; codecs="av01.0.08M.08"',
    'video/mp4; codecs="av01.0.12M.10"', 'video/mp4; codecs="vp09.00.10.08"', 'video/mp4; codecs="mp4a.40.2"',
    'video/mp4; codecs="mp4a.40.5"', 'video/mp4; codecs="ac-3"', 'video/mp4; codecs="ec-3"',
    'video/mp4; codecs="dtsc"', 'video/mp4; codecs="flac"', 'video/mp4; codecs="opus"',
    'video/webm; codecs="vp8"', 'video/webm; codecs="vp9"', 'video/webm; codecs="vp09.02.10.10"',
    'video/webm; codecs="av01.0.08M.08"', 'video/webm; codecs="vorbis"', 'video/webm; codecs="opus"',
    'video/ogg; codecs="theora"', 'audio/ogg; codecs="vorbis"', 'audio/ogg; codecs="opus"',
    'audio/ogg; codecs="flac"', 'audio/mpeg', 'audio/mp4; codecs="mp4a.40.2"', 'audio/mp4; codecs="flac"',
    'audio/mp4; codecs="opus"', 'audio/mp4; codecs="ac-3"', 'audio/wav; codecs="1"', 'audio/aac',
    'audio/flac', 'audio/x-m4a', 'audio/webm; codecs="opus"', 'video/x-matroska; codecs="avc1.42E01E"',
    'video/mp2t', 'application/x-mpegURL', 'application/vnd.apple.mpegurl', 'video/3gpp',
    'video/quicktime', 'audio/x-ms-wma', 'video/x-ms-wmv', 'video/mp4; codecs="dvh1.05.06"',
    'video/mp4; codecs="dvhe.08.07"', 'video/mp4; codecs="vvc1"',
  ];
  const VIDEO_CONFIGS = [
    ['h264-1080p', 'video/mp4; codecs="avc1.640028"', 1920, 1080, 30, 4000000],
    ['h264-4k', 'video/mp4; codecs="avc1.640033"', 3840, 2160, 60, 20000000],
    ['hevc-1080p', 'video/mp4; codecs="hvc1.1.6.L120.B0"', 1920, 1080, 30, 4000000],
    ['hevc-4k-10bit', 'video/mp4; codecs="hvc1.2.4.L153.B0"', 3840, 2160, 60, 20000000],
    ['av1-1080p', 'video/mp4; codecs="av01.0.08M.08"', 1920, 1080, 30, 4000000],
    ['av1-4k-10bit', 'video/mp4; codecs="av01.0.12M.10"', 3840, 2160, 60, 20000000],
    ['vp9-1080p', 'video/webm; codecs="vp09.00.40.08"', 1920, 1080, 30, 4000000],
    ['vp9-4k-hdr', 'video/webm; codecs="vp09.02.51.10.01.09.16.09.01"', 3840, 2160, 60, 20000000],
    ['vp8-720p', 'video/webm; codecs="vp8"', 1280, 720, 30, 2000000],
  ];
  const AUDIO_CONFIGS = [
    ['aac', 'audio/mp4; codecs="mp4a.40.2"'], ['opus', 'audio/webm; codecs="opus"'],
    ['flac', 'audio/flac'], ['eac3', 'audio/mp4; codecs="ec-3"'], ['mp3', 'audio/mpeg'],
    ['dts', 'audio/mp4; codecs="dtsc"'],
  ];
  async function mediaInfo() {
    const video = document.createElement('video');
    const out = { canPlayType: {}, mseSupported: {}, decoding: {}, encoding: {} };
    for (const t of MEDIA_TYPES) {
      out.canPlayType[t] = video.canPlayType(t);
      out.mseSupported[t] = attempt(() => MediaSource.isTypeSupported(t));
    }
    const mc = navigator.mediaCapabilities;
    if (!mc) { out.decoding = null; out.encoding = null; return out; }
    const probe = async (config) => {
      try {
        const r = await mc.decodingInfo(config);
        return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
      } catch (e) { return { error: e.name }; }
    };
    for (const type of ['file', 'media-source']) {
      for (const [name, contentType, width, height, framerate, bitrate] of VIDEO_CONFIGS) {
        out.decoding[`${type}:${name}`] = await probe({ type, video: { contentType, width, height, framerate, bitrate } });
      }
      for (const [name, contentType] of AUDIO_CONFIGS) {
        out.decoding[`${type}:${name}`] = await probe({ type, audio: { contentType, channels: '2', bitrate: 128000, samplerate: 48000 } });
      }
    }
    out.decoding['webrtc:h264'] = await probe({ type: 'webrtc', video: { contentType: 'video/H264; profile-level-id=42e01f', width: 1280, height: 720, framerate: 30, bitrate: 2000000 } });
    out.decoding['webrtc:vp9'] = await probe({ type: 'webrtc', video: { contentType: 'video/VP9', width: 1280, height: 720, framerate: 30, bitrate: 2000000 } });
    out.decoding['webrtc:av1'] = await probe({ type: 'webrtc', video: { contentType: 'video/AV1', width: 1280, height: 720, framerate: 30, bitrate: 2000000 } });
    const enc = async (config) => {
      try {
        const r = await mc.encodingInfo(config);
        return { supported: r.supported, smooth: r.smooth, powerEfficient: r.powerEfficient };
      } catch (e) { return { error: e.name }; }
    };
    for (const [name, contentType] of [['vp8', 'video/webm; codecs="vp8"'], ['vp9', 'video/webm; codecs="vp9"'], ['h264', 'video/mp4; codecs="avc1.42E01E"'], ['av1', 'video/webm; codecs="av01.0.08M.08"']]) {
      out.encoding[`record:${name}`] = await enc({ type: 'record', video: { contentType, width: 1280, height: 720, framerate: 30, bitrate: 2000000 } });
    }
    for (const [name, contentType] of [['h264', 'video/H264; profile-level-id=42e01f'], ['vp8', 'video/VP8'], ['vp9', 'video/VP9'], ['av1', 'video/AV1']]) {
      out.encoding[`webrtc:${name}`] = await enc({ type: 'webrtc', video: { contentType, width: 1280, height: 720, framerate: 30, bitrate: 2000000 } });
    }
    return out;
  }

  const KEY_SYSTEMS = ['com.widevine.alpha', 'com.widevine.alpha.experiment', 'com.microsoft.playready',
    'com.microsoft.playready.recommendation', 'com.apple.fps', 'com.apple.fps.1_0', 'org.w3.clearkey'];
  const ROBUSTNESS = ['', 'SW_SECURE_CRYPTO', 'SW_SECURE_DECODE', 'HW_SECURE_CRYPTO', 'HW_SECURE_DECODE', 'HW_SECURE_ALL'];
  async function emeInfo() {
    if (!navigator.requestMediaKeySystemAccess) return { available: false };
    const out = { available: true, keySystems: {} };
    const ask = async (system, config) => {
      try {
        const access = await withTimeout(navigator.requestMediaKeySystemAccess(system, [config]), 8000, 'timeout');
        if (access === 'timeout') return 'timeout';
        const c = access.getConfiguration();
        return { supported: true, persistentState: c.persistentState, distinctiveIdentifier: c.distinctiveIdentifier, sessionTypes: c.sessionTypes };
      } catch (e) { return { supported: false, error: e.name }; }
    };
    const baseConfig = (robustness, extra = {}) => ({
      initDataTypes: ['cenc'],
      videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"', robustness }],
      audioCapabilities: [{ contentType: 'audio/mp4; codecs="mp4a.40.2"', robustness }],
      ...extra,
    });
    for (const system of KEY_SYSTEMS) {
      const entry = {};
      const levels = system.startsWith('com.widevine') ? ROBUSTNESS : [''];
      for (const r of levels) entry[`robustness:${r || 'none'}`] = await ask(system, baseConfig(r));
      if (system === 'com.widevine.alpha') {
        entry.persistentStateRequired = await ask(system, baseConfig('', { persistentState: 'required' }));
        entry.distinctiveIdentifierRequired = await ask(system, baseConfig('', { distinctiveIdentifier: 'required' }));
        entry.persistentLicense = await ask(system, baseConfig('', { persistentState: 'required', sessionTypes: ['persistent-license'] }));
        entry.hevc = await ask(system, { initDataTypes: ['cenc'], videoCapabilities: [{ contentType: 'video/mp4; codecs="hvc1.1.6.L93.B0"' }] });
        entry.av1 = await ask(system, { initDataTypes: ['cenc'], videoCapabilities: [{ contentType: 'video/mp4; codecs="av01.0.08M.08"' }] });
        entry.vp9 = await ask(system, { initDataTypes: ['cenc'], videoCapabilities: [{ contentType: 'video/webm; codecs="vp9"' }] });
      }
      out.keySystems[system] = entry;
    }
    return out;
  }

  function rtcInfo() {
    if (typeof RTCRtpSender === 'undefined') return { available: false };
    const caps = (obj, kind) => {
      const c = obj.getCapabilities(kind);
      if (!c) return null;
      return {
        codecs: sortedUnique(c.codecs.map((x) => `${x.mimeType}${x.sdpFmtpLine ? ' ' + x.sdpFmtpLine : ''}${x.clockRate ? ' @' + x.clockRate : ''}`)),
        headerExtensions: sortedUnique(c.headerExtensions.map((h) => h.uri)),
      };
    };
    return {
      available: true,
      senderVideo: caps(RTCRtpSender, 'video'), senderAudio: caps(RTCRtpSender, 'audio'),
      receiverVideo: caps(RTCRtpReceiver, 'video'), receiverAudio: caps(RTCRtpReceiver, 'audio'),
      sctp: attempt(() => { const pc = new RTCPeerConnection(); const r = pc.sctp === null ? 'null' : typeof pc.sctp; pc.close(); return r; }),
    };
  }

  async function devicesInfo() {
    const md = navigator.mediaDevices;
    if (!md) return { available: false, secureContext: isSecureContext };
    const list = await withTimeout(md.enumerateDevices(), 5000, []);
    const kinds = { audioinput: 0, audiooutput: 0, videoinput: 0 };
    for (const d of list) kinds[d.kind] = (kinds[d.kind] || 0) + 1;
    return {
      available: true, kinds,
      labeled: list.some((d) => d.label), idsPresent: list.some((d) => d.deviceId),
      supportedConstraints: Object.keys(md.getSupportedConstraints()).sort(),
      getDisplayMedia: typeof md.getDisplayMedia, setCaptureHandleConfig: typeof md.setCaptureHandleConfig,
    };
  }

  async function speechInfo() {
    if (!('speechSynthesis' in window)) return { available: false };
    const synth = speechSynthesis;
    let voices = synth.getVoices();
    if (!voices.length) {
      voices = await new Promise((resolve) => {
        const done = () => resolve(synth.getVoices());
        synth.addEventListener('voiceschanged', done, { once: true });
        setTimeout(done, 1500);
      });
    }
    return {
      available: true,
      voices: voices.map((v) => ({ name: v.name, lang: v.lang, localService: v.localService, default: v.default, voiceURI: v.voiceURI }))
        .sort((a, b) => a.name.localeCompare(b.name)),
    };
  }

  const PERMISSIONS = ['geolocation', 'notifications', 'camera', 'microphone', 'midi', 'clipboard-read',
    'clipboard-write', 'persistent-storage', 'accelerometer', 'gyroscope', 'magnetometer',
    'background-sync', 'background-fetch', 'payment-handler', 'screen-wake-lock', 'idle-detection',
    'window-management', 'local-fonts', 'storage-access', 'top-level-storage-access', 'push',
    'periodic-background-sync', 'nfc', 'display-capture', 'speaker-selection', 'bluetooth',
    'captured-surface-control', 'ambient-light-sensor', 'accessibility-events', 'xr-spatial-tracking',
    'system-wake-lock'];
  async function permissionsInfo() {
    const out = { notification: typeof Notification !== 'undefined' ? Notification.permission : null, query: {} };
    if (!navigator.permissions) return { ...out, available: false };
    for (const name of PERMISSIONS) {
      try {
        const desc = { name };
        if (name === 'push') desc.userVisibleOnly = true;
        const status = await navigator.permissions.query(desc);
        out.query[name] = status.state;
      } catch (e) { out.query[name] = `error:${e.name}`; }
    }
    return out;
  }

  async function storageInfo() {
    const out = { indexedDB: typeof indexedDB, localStorage: attempt(() => typeof localStorage), cookieEnabled: navigator.cookieEnabled };
    if (navigator.storage && navigator.storage.estimate) {
      const est = await navigator.storage.estimate();
      out.quotaGiB = round(est.quota / 2 ** 30, 1);
      out.usageDetails = est.usageDetails ? Object.keys(est.usageDetails).sort() : null;
      out.persisted = navigator.storage.persisted ? await navigator.storage.persisted() : null;
    }
    return out;
  }

  function performanceInfo() {
    const out = {};
    if (performance.memory) {
      out.memory = {
        jsHeapSizeLimit: performance.memory.jsHeapSizeLimit,
        totalJSHeapSize: performance.memory.totalJSHeapSize, usedJSHeapSize: performance.memory.usedJSHeapSize,
      };
    }
    let min = Infinity;
    let last = performance.now();
    for (let i = 0; i < 20000; i++) {
      const now = performance.now();
      if (now > last) { min = Math.min(min, now - last); last = now; }
    }
    out.nowResolutionMs = min === Infinity ? null : round(min, 5);
    out.timeOriginType = typeof performance.timeOrigin;
    out.eventCounts = typeof performance.eventCounts;
    out.measureUserAgentSpecificMemory = typeof performance.measureUserAgentSpecificMemory;
    return out;
  }

  async function batteryInfo() {
    if (!navigator.getBattery) return { available: false };
    const b = await withTimeout(navigator.getBattery(), 3000, null);
    if (!b) return { available: true, error: 'timeout' };
    return { available: true, charging: b.charging, level: b.level, chargingTime: b.chargingTime, dischargingTime: b.dischargingTime };
  }

  function networkInfo() {
    const c = navigator.connection;
    if (!c) return { available: false };
    return { available: true, effectiveType: c.effectiveType, rtt: c.rtt, downlink: c.downlink, downlinkMax: c.downlinkMax ?? null, saveData: c.saveData, type: c.type ?? null };
  }

  async function keyboardInfo() {
    const out = { available: !!navigator.keyboard, layout: null };
    if (navigator.keyboard && navigator.keyboard.getLayoutMap) {
      const map = await withTimeout(navigator.keyboard.getLayoutMap(), 3000, null);
      if (map) {
        out.layout = {};
        for (const [code, key] of [...map.entries()].sort()) out.layout[code] = key;
      }
    }
    return out;
  }

  const CSS_FEATURES = [
    'selector(:has(a))', 'selector(&)', 'selector(:state(x))', 'container-type: inline-size',
    'text-wrap: balance', 'text-wrap: pretty', 'color: color-mix(in srgb, red, blue)',
    'color: oklch(50% 0.1 20)', 'anchor-name: --a', 'position-try-fallbacks: flip-block',
    'view-transition-name: a', 'field-sizing: content', 'text-box-trim: both',
    'interpolate-size: allow-keywords', 'reading-flow: flex-visual', 'corner-shape: squircle',
    'overflow: clip', 'scroll-timeline-name: --x', 'animation-timeline: scroll()',
    'font-palette: light', 'text-spacing-trim: space-first', 'scrollbar-width: thin',
    'scrollbar-color: red blue', 'zoom: 2', 'content-visibility: auto', 'overlay: auto',
    'white-space-collapse: preserve', 'text-autospace: normal', 'anchor-scope: all',
    'scroll-marker-group: before', 'position-area: center', 'display: masonry',
    'font-variant-emoji: emoji', 'text-emphasis: filled', 'math-depth: 1', 'ruby-align: space-around',
    'width: calc-size(auto, size)', 'width: stretch', 'width: -webkit-fill-available',
    'backdrop-filter: blur(1px)', 'accent-color: red', 'color-scheme: dark', 'forced-color-adjust: none',
    'contain-intrinsic-size: auto 1px', 'offset-path: shape(from 0 0, line to 1px 1px)',
    'line-clamp: 2', 'text-decoration-skip-ink: auto', 'hyphenate-limit-chars: 6',
  ];
  function cssInfo() {
    const out = { supports: {} };
    for (const f of CSS_FEATURES) {
      out.supports[f] = f.startsWith('selector(') ? attempt(() => CSS.supports(f), 'error') : CSS.supports(f);
    }
    out.highlights = typeof CSS.highlights;
    out.registeredProperties = typeof CSS.registerProperty;
    out.paintWorklet = typeof CSS.paintWorklet;
    out.layoutWorklet = typeof CSS.layoutWorklet;
    out.animationWorklet = typeof CSS.animationWorklet;
    out.units = attempt(() => ['dvh', 'svh', 'lvh', 'cqw', 'rlh', 'cap', 'ic', 'lh', 'vb', 'vi', 'rex', 'rch'].filter((u) => CSS.supports('width', `1${u}`)));
    return out;
  }

  function chromeObjectInfo() {
    const c = window.chrome;
    if (!c) return { present: false };
    return {
      present: true, keys: Object.keys(c).sort(), ownNames: Object.getOwnPropertyNames(c).sort(),
      runtime: typeof c.runtime, loadTimes: typeof c.loadTimes, csi: typeof c.csi,
      app: c.app ? Object.keys(c.app).sort() : null,
      appIsInstalled: c.app ? attempt(() => c.app.isInstalled) : null,
    };
  }

  function miscInfo() {
    return {
      documentFeaturePolicy: attempt(() => (document.featurePolicy || document.permissionsPolicy).allowedFeatures().sort()),
      historyLength: history.length,
      notificationMaxActions: attempt(() => Notification.maxActions),
      userActivation: typeof navigator.userActivation,
      schedulingIsInputPending: typeof (navigator.scheduling && navigator.scheduling.isInputPending),
      getInstalledRelatedApps: typeof navigator.getInstalledRelatedApps,
      share: typeof navigator.share, canShare: typeof navigator.canShare,
      bluetooth: typeof navigator.bluetooth, usb: typeof navigator.usb, hid: typeof navigator.hid,
      serial: typeof navigator.serial, xr: typeof navigator.xr, ink: typeof navigator.ink,
      virtualKeyboard: typeof navigator.virtualKeyboard, windowControlsOverlay: typeof navigator.windowControlsOverlay,
      login: typeof navigator.login, gpu: typeof navigator.gpu, ml: typeof navigator.ml,
      devicePosture: typeof navigator.devicePosture, locks: typeof navigator.locks,
      contacts: typeof navigator.contacts, presentation: typeof navigator.presentation,
      wakeLock: typeof navigator.wakeLock, mediaSession: typeof navigator.mediaSession,
      credentials: typeof navigator.credentials, managed: typeof navigator.managed,
      adAuctionComponents: typeof navigator.adAuctionComponents, runAdAuction: typeof navigator.runAdAuction,
      joinAdInterestGroup: typeof navigator.joinAdInterestGroup, sharedStorage: typeof window.sharedStorage,
      browsingTopics: typeof document.browsingTopics, fencedFrameConfig: typeof window.FencedFrameConfig,
      openDatabase: typeof window.openDatabase, showDirectoryPicker: typeof window.showDirectoryPicker,
      queryLocalFonts: typeof window.queryLocalFonts, launchQueue: typeof window.launchQueue,
      documentPictureInPicture: typeof window.documentPictureInPicture,
      getScreenDetails: typeof window.getScreenDetails, cookieStore: typeof window.cookieStore,
      trustedTypes: typeof window.trustedTypes, scheduler: typeof window.scheduler,
      navigation: typeof window.navigation, caches: typeof window.caches,
      visibilityState: document.visibilityState, hasFocus: document.hasFocus(),
      hidden: document.hidden,
    };
  }

  function workerInfo() {
    if (typeof Worker === 'undefined') return Promise.resolve({ available: false });
    const source = `
      ${fnv1a64.toString()}
      ${hashBuffer.toString()}
      ${drawTextScene.toString()}
      ${drawGeometryScene.toString()}
      self.onmessage = () => {
        const out = {};
        try {
          out.userAgent = navigator.userAgent; out.platform = navigator.platform;
          out.appVersion = navigator.appVersion;
          out.hardwareConcurrency = navigator.hardwareConcurrency;
          out.deviceMemory = navigator.deviceMemory ?? null;
          out.language = navigator.language; out.languages = [...navigator.languages];
          out.timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
          out.uaDataPlatform = navigator.userAgentData ? navigator.userAgentData.platform : null;
          out.uaDataBrands = navigator.userAgentData ? navigator.userAgentData.brands : null;
          out.globals = Object.getOwnPropertyNames(self).sort();
        } catch (e) { out.error = String(e); }
        try {
          const c = new OffscreenCanvas(256, 128);
          const gl = c.getContext('webgl');
          if (gl) {
            const d = gl.getExtension('WEBGL_debug_renderer_info');
            out.webgl = {
              unmaskedVendor: d ? gl.getParameter(d.UNMASKED_VENDOR_WEBGL) : null,
              unmaskedRenderer: d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : null,
              maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
              extensions: (gl.getSupportedExtensions() || []).slice().sort(),
            };
          } else out.webgl = null;
        } catch (e) { out.webgl = { error: String(e) }; }
        try {
          out.canvas = {};
          for (const [name, w, h, draw] of [['text', 240, 60, drawTextScene], ['geometry', 122, 110, drawGeometryScene]]) {
            const c = new OffscreenCanvas(w, h);
            const ctx = c.getContext('2d');
            draw(ctx);
            out.canvas[name] = hashBuffer(ctx.getImageData(0, 0, w, h).data);
          }
        } catch (e) { out.canvas = { error: String(e) }; }
        postMessage(out);
      };`;
    const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
    return new Promise((resolve) => {
      const worker = new Worker(url);
      const timer = setTimeout(() => { worker.terminate(); resolve({ available: true, error: 'timeout' }); }, 8000);
      worker.onmessage = (e) => { clearTimeout(timer); worker.terminate(); URL.revokeObjectURL(url); resolve({ available: true, ...e.data }); };
      worker.onerror = (e) => { clearTimeout(timer); worker.terminate(); resolve({ available: true, error: e.message }); };
      worker.postMessage('go');
    });
  }

  function iframeInfo(w) {
    const out = {
      userAgent: w.navigator.userAgent, platform: w.navigator.platform,
      hardwareConcurrency: w.navigator.hardwareConcurrency, deviceMemory: w.navigator.deviceMemory ?? null,
      languages: [...w.navigator.languages], webdriver: w.navigator.webdriver,
      timeZone: w.Intl.DateTimeFormat().resolvedOptions().timeZone,
      screenWidth: w.screen.width, devicePixelRatio: w.devicePixelRatio,
    };
    const canvas = w.document.createElement('canvas');
    canvas.width = 240; canvas.height = 60;
    const ctx = canvas.getContext('2d');
    drawTextScene(ctx);
    out.canvasTextHash = hashBuffer(ctx.getImageData(0, 0, 240, 60).data);
    const glCanvas = w.document.createElement('canvas');
    const gl = glCanvas.getContext('webgl');
    if (gl) {
      const d = gl.getExtension('WEBGL_debug_renderer_info');
      out.webglRenderer = d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : null;
      attempt(() => gl.getExtension('WEBGL_lose_context').loseContext());
    } else out.webglRenderer = null;
    return out;
  }

  // ------------------------------------------------------------------ driver
  const ui = {
    status: document.getElementById('status'),
    out: document.getElementById('out'),
    fonts: document.getElementById('local-fonts'),
    download: document.getElementById('download'),
    insecure: document.getElementById('insecure'),
  };
  let record = null;
  const captureId = Math.random().toString(36).slice(2) + Date.now().toString(36);

  function setStatus(text) {
    if (ui.status) ui.status.textContent = text;
  }
  function render() {
    const json = JSON.stringify(record, null, 1);
    if (ui.out) ui.out.textContent = json;
    if (ui.download) {
      ui.download.href = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
      ui.download.download = `fingerprint-${captureId}.json`;
      ui.download.hidden = false;
    }
    document.documentElement.dataset.captureState = 'done';
  }
  async function submit() {
    try {
      const res = await fetch('submit', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(record),
      });
      setStatus(res.ok ? `Captured and submitted (${(performance.now() - t0) / 1000 | 0}s). You can close this tab.` : `Captured; submit failed with HTTP ${res.status}. Use the download link.`);
    } catch (e) {
      setStatus('Captured; no server to submit to. Use the download link.');
    }
  }

  async function run() {
    if (!isSecureContext && ui.insecure) ui.insecure.hidden = false;
    setStatus('Capturing…');
    const frame = cleanRealm();
    const w = frame.contentWindow;
    const steps = [
      ['navigator', () => navigatorInfo()],
      ['ua_data', uaDataInfo],
      ['headers', headersInfo],
      ['screen', () => screenInfo()],
      ['media_queries', () => mediaQueriesInfo()],
      ['intl', () => intlInfo()],
      ['api_surface', () => apiSurfaceInfo(w)],
      ['js_engine', () => jsEngineInfo(w)],
      ['webgl', () => webglInfo('webgl')],
      ['webgl2', () => webglInfo('webgl2')],
      ['webgpu', webgpuInfo],
      ['canvas', () => canvasInfo()],
      ['text_metrics', () => textMetricsInfo()],
      ['emoji', () => emojiInfo()],
      ['fonts', () => fontsInfo()],
      ['form_controls', () => formControlsInfo(w)],
      ['default_fonts', () => defaultFontsInfo(w)],
      ['audio', audioInfo],
      ['media', mediaInfo],
      ['eme', emeInfo],
      ['rtc', () => rtcInfo()],
      ['devices', devicesInfo],
      ['speech', speechInfo],
      ['permissions', permissionsInfo],
      ['storage', storageInfo],
      ['performance', () => performanceInfo()],
      ['battery', batteryInfo],
      ['network', () => networkInfo()],
      ['keyboard', keyboardInfo],
      ['css', () => cssInfo()],
      ['chrome_object', () => chromeObjectInfo()],
      ['misc', () => miscInfo()],
      ['worker', workerInfo],
      ['iframe', () => iframeInfo(w)],
    ];
    record = { meta: { schema: SCHEMA, captureId, capturedAt: new Date().toISOString(), url: location.href, secureContext: isSecureContext } };
    for (const [name, fn] of steps) {
      setStatus(`Capturing ${name}…`);
      record[name] = await section(fn);
    }
    frame.remove();
    record.meta.durationMs = Math.round(performance.now() - t0);
    render();
    await submit();
    if (ui.fonts && typeof window.queryLocalFonts === 'function') ui.fonts.hidden = false;
  }

  if (ui.fonts) {
    ui.fonts.addEventListener('click', async () => {
      try {
        const fonts = await window.queryLocalFonts();
        record.fonts.local = sortedUnique(fonts.map((f) => f.family));
        record.fonts.localFaces = fonts.length;
        record.meta.resubmission = true;
        render();
        await submit();
        ui.fonts.disabled = true;
      } catch (e) { setStatus(`Local fonts: ${e.name} — ${e.message}`); }
    });
  }

  run().catch((e) => setStatus(`Capture failed: ${e}`));
})();
