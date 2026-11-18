'use strict';
// 成绩单前端测试辅助脚本，由 tests/test_frontend_report_scores.py 调用。
//
// 在 Node 的 vm 沙箱里运行真实的 frontend/app.js（?debug=1 会挂出数据通道入口
// window.__interviewDebug.data），用极简的假 DOM 接收成绩单消息，再把成绩单相关节点的
// 文字与填充宽度打印成 JSON。不联网，不跑定时器。
//
// 用法：node frontend_report_harness.js <app.js 路径>
//   stdin : {"场景名": 数据通道消息, ...}
//   stdout: {"场景名": {score, grade, summary, dims, fills, structured, raw, rawVisible}, ...}

const fs = require('fs');
const vm = require('vm');

const DIM_KEYS = ['profession_know', 'exp_logic', 'comper_quality'];

function noop() {}

function createFakeElement(tag, id) {
  const base = {
    tagName: String(tag || 'div').toUpperCase(),
    id: id || '',
    textContent: '',
    hidden: false,
    value: '',
    disabled: false,
    dataset: {},
    style: {
      props: {},
      setProperty(name, value) { this.props[name] = String(value); },
      removeProperty: noop
    },
    classList: { add: noop, remove: noop, toggle: noop, contains: function () { return false; } },
    children: [],
    appendChild(child) { base.children.push(child); return child; },
    removeChild(child) {
      base.children = base.children.filter(function (item) { return item !== child; });
      return child;
    },
    replaceChildren() { base.children = []; },
    setAttribute: noop,
    removeAttribute: noop,
    addEventListener: noop,
    removeEventListener: noop,
    focus: noop,
    blur: noop,
    scrollIntoView: noop,
    getBoundingClientRect: function () {
      return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 };
    },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    closest: function () { return null; }
  };
  // 其余未列出的属性和方法一律当作空操作：这里只保证成绩单这条路径能跑通。
  return new Proxy(base, {
    get: function (target, prop, receiver) {
      if (prop in target || typeof prop === 'symbol' || prop === 'then') {
        return Reflect.get(target, prop, receiver);
      }
      return noop;
    }
  });
}

function createFakeDocument() {
  const elements = new Map();
  const documentFake = {
    getElementById: function (id) {
      if (!elements.has(id)) elements.set(id, createFakeElement('div', id));
      return elements.get(id);
    },
    createElement: function (tag) { return createFakeElement(tag, ''); },
    createElementNS: function (namespace, tag) { return createFakeElement(tag, ''); },
    createTextNode: function (text) { return createFakeElement('#text', ''); },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    addEventListener: noop,
    removeEventListener: noop,
    documentElement: createFakeElement('html', ''),
    body: createFakeElement('body', ''),
    activeElement: null
  };
  return { documentFake: documentFake, elements: elements };
}

function createSandbox(documentFake) {
  const sandbox = {
    console: { log: noop, info: noop, warn: noop, error: noop, debug: noop },
    document: documentFake,
    // localhost 不会触发公网静态站的演示跳转；?debug=1 挂出数据通道入口，非演示模式。
    location: { hostname: 'localhost', pathname: '/', search: '?debug=1', hash: '', replace: noop },
    URLSearchParams: URLSearchParams,
    TextDecoder: TextDecoder,
    innerWidth: 1024,
    // 不跑定时器，因此不会去加载 LiveKit；动画同步完成。
    setTimeout: function () { return 0; },
    clearTimeout: noop,
    requestAnimationFrame: function (callback) { callback(Infinity); return 0; },
    // 不联网：健康检查和学习者档案请求永远不结束。
    fetch: function () { return new Promise(noop); },
    addEventListener: noop,
    removeEventListener: noop
  };
  sandbox.window = sandbox;
  return vm.createContext(sandbox);
}

function runReport(appSource, message) {
  const fake = createFakeDocument();
  const context = createSandbox(fake.documentFake);
  vm.runInContext(appSource, context, { filename: 'app.js' });
  const hook = context.window.__interviewDebug;
  if (!hook) throw new Error('app.js 没有挂出 __interviewDebug');
  hook.data(message);

  const node = function (id) { return fake.elements.get(id); };
  const text = function (id) {
    const element = node(id);
    return element ? String(element.textContent) : '';
  };
  const dims = {};
  const fills = {};
  DIM_KEYS.forEach(function (key) {
    dims[key] = text('dimValue-' + key);
    const fill = node('dimFill-' + key);
    fills[key] = fill ? fill.style.props['--pct'] : undefined;
  });
  const reportDims = node('reportDims');
  const reportRaw = node('reportRaw');
  return {
    score: text('scoreValue'),
    grade: text('reportGrade'),
    summary: text('reportSummary'),
    dims: dims,
    fills: fills,
    structured: Boolean(reportDims) && !reportDims.hidden,
    raw: text('reportRaw'),
    rawVisible: Boolean(reportRaw) && !reportRaw.hidden
  };
}

function main() {
  const appSource = fs.readFileSync(process.argv[2], 'utf8');
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', function (chunk) { input += chunk; });
  process.stdin.on('end', function () {
    const scenarios = JSON.parse(input || '{}');
    const out = {};
    Object.keys(scenarios).forEach(function (name) {
      out[name] = runReport(appSource, scenarios[name]);
    });
    process.stdout.write(JSON.stringify(out));
  });
}

main();
