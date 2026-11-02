# 第 7 章：浏览器工作台（frontend/）

前端的目标不是“好看”，而是让考生在**没有说明书**的情况下知道：现在谁在说话、进行到哪、什么时候该回答、结束后看什么。

它刻意保持三个约束：

1. **无构建步骤**：没有 npm、没有打包器；`index.html` 直接加载 `app.js`。
2. **无框架**：一个 IIFE + 原生 DOM。
3. **后端数据当不可信**：所有动态文本用 `textContent`，不用 `innerHTML`。

---

## 7.1 文件分工

| 文件 | 职责 |
| --- | --- |
| `index.html` | 结构、SVG 图标精灵、无障碍属性 |
| `styles.css` | 深色主题、响应式、动效、状态样式 |
| `app.js` | 连接、状态、转写、成绩单、演示模式 |

### `index.html` 的骨架

```text
body[data-conn]
├── svg.svg-sprite            # 所有图标 <symbol>
├── .shell
│   ├── header.topbar         # 品牌、连接质量、计时、状态徽章
│   ├── main.lobby            # 大厅：标题 + 模式选择
│   ├── main.session          # 考场
│   │   ├── .session-head     # 阶段标题 + 4 步进度条
│   │   └── .session-body
│   │       ├── .stage        # AI 考官卡 + 考生卡
│   │       └── .conversation # 转写区 + 错误卡
│   └── footer.controlbar     # 麦克风、成绩单、结束、进入
├── #demoBadge                # 演示模式角标
├── #playbackBanner           # 自动播放被拦截提示
├── #reportLayer              # 成绩单弹窗
├── #toastRegion
└── <noscript>
```

CSS 通过 `body[data-conn="..."]`、`body[data-phase="..."]` 切换布局/状态，而不是让 JS 到处加 class。

图标全部内联为 `<symbol>`，用 `<use href="#i-mic">` 引用：

- 无额外网络请求；
- `currentColor` 自动跟随文字颜色；
- 不依赖图标字体。

---

## 7.2 `app.js` 的组织方式

文件开头有一个 IIFE：

```js
(function () {
  'use strict';
  ...
  init();
})();
```

整个应用只有一个模块作用域，避免污染 `window`。内部按注释分成这些区：

```text
环境/常量 → DOM 引用 → 运行时状态 → 小工具
→ Toast/错误 → 连接徽章 → 阶段条 → AI 状态
→ 计时器 → 转写 → 数据通道 → 成绩单
→ 音频播放 → 房间事件 → 连接/断开
→ 演示模式 → 页面生命周期 → 事件绑定 → 启动
```

阅读时建议按这个顺序，不要从 `init()` 往下跳。

---

## 7.3 常量与运行时状态

```js
const PHASES = [
  { key: 'self_intro', label: '自我介绍' },
  { key: 'pro_qa', label: '专业问答' },
  { key: 'compre_qa', label: '综合素质' },
  { key: 'report', label: '成绩单' }
];
const PHASE_LABELS = {
  self_intro: '自我介绍',
  pro_qa: '专业问答',
  compre_qa: '综合素质',
  report: '综合成绩单',
  completed: '面试结束'
};
const DIM_KEYS = ['profession_know', 'exp_logic', 'comper_quality'];
```

`PHASES` 驱动进度条；`PHASE_LABELS` 驱动文案；`DIM_KEYS` 驱动成绩单三个维度。加维度时三处一起改。

```js
const state = {
  conn: 'idle',
  phase: null,
  aiStatus: 'idle',
  micEnabled: false,
  starting: false,
  room: null,
  localIdentity: '',
  report: null,
  demoActive: false,
  timerId: null,
  timerStart: 0,
  thinkingTimer: null,
  pageHiding: false
};
```

`state` 只放“跨函数共享”的东西；纯局部的 DOM 引用、定时器句柄放在闭包里。

另外几个模块级变量：

| 变量 | 作用 |
| --- | --- |
| `segments` | `Map<key, {el, textEl}>`，转写分片原位更新 |
| `audioEls` | `Map<sid, HTMLAudioElement>`，音频轨道清理 |
| `demoTimers` | 演示模式的定时器，重启时统一清 |
| `autoScroll` | 用户是否在底部；上翻时暂停自动滚动 |
| `retryAction` | 错误卡“重试”按钮要执行的动作 |
| `textDecoder` | UTF-8 解码 data channel |

---

## 7.4 小工具

```js
function setText(node, text) {
  if (!node) return;
  const next = text == null ? '' : String(text);
  if (node.textContent !== next) node.textContent = next;
}
```

先比较再赋值，避免无意义的 DOM 变更和屏幕阅读器重复播报。

```js
function toggle(node, visible) { if (node) node.hidden = !visible; }
function clamp(value, min, max) { ... }
function prefersReducedMotion() { return matchMedia('(prefers-reduced-motion: reduce)').matches; }
function createIcon(name) { ... }   // 创建 <svg><use href="#..."/></svg>
function clockNow() { ... }         // HH:MM:SS
function formatDuration(ms) { ... } // mm:ss
```

`prefersReducedMotion()` 在演示打字、数字滚动、成绩单环形动画里都会用到。

---

## 7.5 连接状态机

前端自己的连接状态：

```text
idle → connecting → connected → reconnecting → connected
                        │
                        ├── error
                        └── ended
```

`setConn(conn)` 做四件事：

1. 写 `state.conn` 和 `body.dataset.conn`（CSS 用）；
2. 更新顶部状态徽章；
3. 显示/隐藏“进行中”标签、空态；
4. 调 `updateHints()` 和 `syncControls()`。

`syncControls()` 决定按钮可用性：

| 控件 | 可用条件 |
| --- | --- |
| 进入考场 | 未连接、未连接中、未重连 |
| 结束面试 | connected / reconnecting |
| 成绩单 | `state.report` 存在 |
| 麦克风 | connected |
| 模式选择 | 未开始面试（`modeIsLocked()`） |

`syncMicUI()` 同时更新底部麦克风按钮和考生卡上的按钮，保证两处状态一致。

---

## 7.6 阶段进度条

```js
function setPhase(phase, silent) {
  ...
  const activeIndex = completed || !phase
    ? -1
    : PHASES.findIndex(item => item.key === phase);

  stepNodes.forEach((node, index) => {
    const done = completed || (activeIndex >= 0 && index < activeIndex);
    const active = activeIndex === index;
    node.classList.toggle('is-done', done);
    node.classList.toggle('is-active', active);
    ...
  });

  let progress = 0;
  if (completed) progress = 1;
  else if (activeIndex >= 0) progress = activeIndex / (PHASES.length - 1);
  stepperFill.style.setProperty('--progress', String(progress));
  ...
  document.body.dataset.phase = phase || '';
}
```

几个细节：

- `completed` 时所有步骤都标完成，进度条 100%；
- `report` 是第 4 步，成绩单生成时高亮；
- `silent=true` 用于初始化/重置，不往转写区写“进入阶段”；
- `body.dataset.phase` 让 CSS 可以按阶段改标题/布局。

后端只发 `phase` 事件；前端不自己猜阶段。

---

## 7.7 AI 状态与说话指示

```js
function setAiStatus(status) {
  state.aiStatus = status;
  aiTile.dataset.status = status;
  setText(aiStateText, AI_STATUS_TEXT[status] || AI_STATUS_TEXT.idle);
  aiTile.classList.toggle('is-thinking', status === 'thinking');
  aiTile.classList.toggle('is-speaking', status === 'speaking');
  aiTile.classList.toggle('is-listening', status === 'listening');
}
```

状态来源有两类：

1. **后端 `status` 事件**：thinking / speaking / listening；
2. **LiveKit `ActiveSpeakersChanged`**：谁在发声。

`handleActiveSpeakers` 优先相信“有人正在说话”：

```js
if (aiSpeaking) setAiStatus('speaking');
else if (state.aiStatus === 'speaking') setAiStatus('listening');
```

`markCandidateFinished()` 在考生转写 `final` 后把 AI 标成 thinking，并设 12 秒兜底：如果后端一直没回，回到 listening，避免 UI 永远转圈。

---

## 7.8 转写：分片原位更新

LiveKit 的 `TranscriptionReceived` 会给一串 segment，每个有 `id`、`text`、`final`。同一句话会先来多个 partial，最后来一个 final。

```text
segment id = "abc"
  partial "我认为"      → 创建一行，文本 "我认为"
  partial "我认为大模型" → 找到同一行，替换文本
  final   "我认为大模型幻觉…" → 标 is-final
```

实现：

```js
const key = identity + '|' + rawId;
upsertSegment(role, key, segment.text, Boolean(segment.final));
```

`upsertSegment`：

```js
let record = segments.get(safeKey);
if (!record) {
  if (!clean) return null;
  record = createTranscriptLine(role);
  segments.set(safeKey, record);
  appendTranscript(record.el);
}
if (clean || !isFinal) setText(record.textEl, clean);
if (isFinal) { ... remove is-partial, add is-final ... }
else { record.el.classList.add('is-partial'); }
```

角色判断：

```js
const isCandidate = identity && localIdentity && identity === localIdentity;
const role = isCandidate ? 'candidate' : 'examiner';
```

`localIdentity` 来自 `/api/token` 响应的 `identity`，或 `room.localParticipant.identity`。

### 自动滚动

```js
function handleTranscriptScroll() {
  const distance = scrollHeight - scrollTop - clientHeight;
  const atBottom = distance < 56;
  if (atBottom !== autoScroll) {
    autoScroll = atBottom;
    toggle(jumpLatest, !autoScroll);
  }
}
```

用户上翻时暂停自动滚动，并显示“回到最新”按钮；回到底部自动恢复。`scheduleScroll()` 用 `requestAnimationFrame` 合并滚动，避免每个 token 都触发。

### 安全

所有文本都走 `textContent`：

```js
text.textContent = clean;
```

即使 LLM 输出 `<img src=x onerror=...>`，也只会显示成文本。转写区 `role="log"`、`aria-live="polite"`，屏幕阅读器会朗读新增内容。

---

## 7.9 数据通道

```js
function handleDataPacket(payload, topic) {
  if (topic && String(topic) !== 'interview') return;
  const raw = decodePayload(payload);
  const message = JSON.parse(raw);
  handleDataMessage(message);
}
```

`decodePayload` 兼容三种形态：

- 字符串；
- `Uint8Array` / `ArrayBuffer`；
- 有 `byteLength` 的类数组。

`handleDataMessage` 按 `type` 分发：

```js
if (type === 'status') { ... }
if (type === 'phase')  { ... }
if (type === 'report') { handleReportMessage(message); }
// 未知类型忽略，保持向前兼容
```

未知类型**故意忽略**：后端以后加新事件，旧前端不会崩。

---

## 7.10 成绩单

### 规范化

```js
function normalizeReport(raw) {
  const scores = raw.scores && typeof raw.scores === 'object' ? raw.scores : null;
  const totalRaw = Number(raw.total_score);
  if (!scores && !Number.isFinite(totalRaw)) return null;

  const dims = {
    profession_know: toScore(scores ? scores.profession_know : 0),
    ...
  };
  const average = (dims.profession_know + dims.exp_logic + dims.comper_quality) / 3;
  const total = Number.isFinite(totalRaw) ? clamp(totalRaw, 0, 100) : Math.round(average);
  return { total, dims, weakness, tips, closing };
}
```

后端已经规范化过，这里再兜一层，防止旧版本/手工事件带来坏数据。

### 渲染

- `animateNumber`：0 → 总分，缓动 900ms；`prefersReducedMotion` 时直接显示。
- `animateRing`：SVG 环形进度，`strokeDashoffset` 从满到 `(1 - pct)`。
- 三个维度条：`--pct` 自定义属性 + `transition-delay` 错开。
- 所有文本 `textContent`。

### 打开/关闭

```js
function openReport() {
  applyStructuredReport(...) 或 applyRawReport(...);
  lastFocus = document.activeElement;
  reportLayer.hidden = false;
  reportLayer.setAttribute('aria-hidden', 'false');
  body.classList.add('modal-open');
  requestAnimationFrame(() => reportLayer.classList.add('is-open'));
  // 焦点移到关闭按钮
}
```

`closeReport` 恢复焦点到打开前的元素。`handleReportKeydown` 处理：

- Escape 关闭；
- Tab 在弹窗内循环（焦点陷阱）。

### 重复事件

后端适配器已经保证 `report` 只发一次。前端仍然可以重复调用 `openReport()`（用户点“成绩单”按钮），因为 `state.report` 被保存了。

---

## 7.11 音频播放

```js
function attachAudioTrack(track) {
  const element = track.attach();
  element.autoplay = true;
  element.setAttribute('playsinline', '');
  audioSink.appendChild(element);
  audioEls.set(sid, element);
  const promise = element.play();
  if (promise && promise.catch) {
    promise.catch(() => showPlaybackPrompt(true));
  }
}
```

浏览器可能拦截自动播放（尤其是用户还没交互时）。这时显示底部横幅“点击以开启考官语音”，`restorePlayback()` 调 `room.startAudio()`。

`detachAudioTrack` / `cleanupAudio` 在轨道取消、退出、页面隐藏时暂停并移除 `<audio>`，避免内存泄漏和“退出后还在说话”。

---

## 7.12 连接 LiveKit

### 懒加载 SDK

```js
const LIVEKIT_CDN = 'https://cdn.jsdelivr.net/npm/livekit-client@2.11.0/dist/livekit-client.umd.min.js';

function loadLiveKit() {
  if (window.LivekitClient) { ... return Promise.resolve(LK); }
  if (liveKitPromise) return liveKitPromise;
  liveKitPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = LIVEKIT_CDN;
    script.async = true;
    script.onload = () => { ... };
    script.onerror = () => { ... };
    document.head.appendChild(script);
  });
  return liveKitPromise;
}
```

为什么不用 `<script src>` 直接放 `<head>`：

- 阻塞首屏；
- CDN 挂了页面白屏；
- 演示模式根本不需要 SDK。

`init()` 在 1.5 秒后预加载，失败也不影响演示模式。

### `startInterview()`

```text
1. 防重入（state.starting）
2. 演示模式 → restartDemo()
3. 清错误、清成绩单、连接状态 connecting
4. loadLiveKit()
5. fetch('/api/token?room=ai-room&mode=...')
6. 用服务端返回的 interviewMode 校准本地选择
7. new LK.Room({adaptiveStream:true, dynacast:true})
8. wireRoom(room)
9. await room.connect(serverUrl, token)
10. setConn('connected')、启动计时
11. room.startAudio()
12. localParticipant.setMicrophoneEnabled(true)
```

错误分两类：

- SDK 加载失败 → 提示“检查网络，或用 ?demo=1”；
- token/连接失败 → `friendlyConnectionError` 区分网络错误和服务器错误。

### `wireRoom`

监听：

| 事件 | 处理 |
| --- | --- |
| `TrackSubscribed` | 音频轨道挂载 |
| `TrackUnsubscribed` | 清理音频 |
| `ActiveSpeakersChanged` | AI/考生说话状态 |
| `AudioPlaybackStatusChanged` | 自动播放横幅 |
| `ConnectionQualityChanged` | 顶部信号格 |
| `DataReceived` | UI 事件 |
| `TranscriptionReceived` | 转写 |
| `ParticipantConnected/Disconnected` | 系统提示 |
| `Reconnecting/Reconnected` | 重连状态 |
| `Disconnected` | 清理 + 错误卡 |
| `MediaDevicesError` | 麦克风错误 + 重试 |

### 麦克风

`enableMic()` 和 `toggleMic()` 都调 `room.localParticipant.setMicrophoneEnabled(...)`，成功后同步 `state.micEnabled` 和 UI。失败时用 `describeMediaError` 把浏览器错误翻译成中文：

| 错误名 | 文案 |
| --- | --- |
| `NotAllowedError` | 浏览器拒绝了麦克风权限 |
| `NotFoundError` | 未检测到麦克风 |
| `NotReadableError` | 麦克风被其他程序占用 |

---

## 7.13 演示模式

URL 参数：

```text
?demo=1                  完整自动流程（quick）
?demo=1&mode=real        实战模式脚本
?demo=1&scene=report     直接展示成绩单
?demo=1&scene=connected  展示“进行中”静态场景
```

### 为什么需要它

- 没有 LiveKit / API Key 也能录屏、评审 UI；
- 前端改动可以快速回归；
- 面试流程/文案可以在本地完整走一遍。

### 自动流程

```js
const script = DEMO_MODE_SCRIPT[interviewMode] || DEMO_MODE_SCRIPT.quick;
```

每个 item：

```js
{ phase, examiner, candidate, followUp?, followUpAnswer? }
```

`initDemoAutoScene()` 依次：

1. 考官打字 → 考生打字；
2. 如果有 `followUp`，再考官追问 → 考生回答；
3. 下一项；
4. 脚本结束 → 1.8 秒后弹成绩单。

`demoType` 用定时器逐 3 个字符更新同一行，模拟 partial 转写；`prefersReducedMotion` 时直接整段。

### 演示模式的边界

它**不产生真实音频**，也不采集麦克风。所有状态都是本地脚本，所以适合 UI 演示，不能验证 STT/TTS 质量。

---

## 7.14 页面生命周期

```js
function handlePageHide() {
  if (state.pageHiding) return;
  state.pageHiding = true;
  clearDemoTimers();
  stopTimer();
  cleanupAudio();
  const room = state.room;
  state.room = null;
  if (room) { try { room.disconnect(); } catch {} }
}
window.addEventListener('pagehide', handlePageHide);
window.addEventListener('beforeunload', handlePageHide);
```

为什么两个事件都监听：不同浏览器/移动端触发的时机不同。`pageHiding` 防重复。

---

## 7.15 函数索引

| 区 | 函数 | 作用 |
| --- | --- | --- |
| 工具 | `setText` / `toggle` / `clamp` | DOM/数值小工具 |
| 工具 | `prefersReducedMotion` | 动效降级 |
| 工具 | `createIcon` / `clockNow` / `formatDuration` | 图标/时间 |
| 错误 | `showToast` / `showError` / `hideError` | 通知与内联错误 |
| 错误 | `describeMediaError` | 麦克风错误翻译 |
| 连接 | `setConn` / `updateHints` | 连接状态与提示 |
| 连接 | `modeIsLocked` / `syncModeUI` / `setMode` / `selectMode` | 模式选择 |
| 连接 | `syncControls` / `syncMicUI` | 按钮状态 |
| 阶段 | `setPhase` | 进度条/标题/body dataset |
| 状态 | `setAiStatus` / `setTileSpeaking` | AI 状态 |
| 状态 | `markCandidateFinished` / `markExaminerFinished` | 转写结束后的状态 |
| 状态 | `handleActiveSpeakers` / `updateQuality` | 说话人/质量 |
| 计时 | `startTimer` / `stopTimer` | 会话计时 |
| 转写 | `updateTranscriptCount` / `scheduleScroll` / `appendTranscript` | 计数/滚动 |
| 转写 | `createTranscriptLine` / `logSystem` / `upsertSegment` | 行创建/更新 |
| 转写 | `getLocalIdentity` / `handleTranscription` / `handleTranscriptScroll` | 角色/输入/滚动 |
| 数据 | `decodePayload` / `handleDataPacket` / `handleDataMessage` | 事件分发 |
| 报告 | `toScore` / `normalizeReport` / `gradeFor` / `roundScore` | 规范化 |
| 报告 | `animateNumber` / `resetRingVisual` / `animateRing` | 动画 |
| 报告 | `setReportSectionsVisible` / `applyStructuredReport` / `applyRawReport` / `resetReportUI` | 渲染 |
| 报告 | `handleReportMessage` / `openReport` / `closeReport` / `handleReportKeydown` | 弹窗 |
| 音频 | `isAudioTrack` / `attachAudioTrack` / `detachAudioTrack` / `cleanupAudio` / `showPlaybackPrompt` | 播放 |
| 房间 | `onRoomEvent` / `handleRoomDisconnected` / `wireRoom` | 事件 |
| 连接 | `loadLiveKit` / `startInterview` / `friendlyConnectionError` | 连接 |
| 连接 | `enableMic` / `toggleMic` / `restorePlayback` | 设备 |
| 连接 | `teardownSession` / `leaveSession` / `restartInterview` | 清理 |
| 演示 | `clearDemoTimers` / `estimateTypingDuration` / `demoType` | 打字 |
| 演示 | `demoSeedConnectedScene` / `initDemoConnectedScene` / `initDemoAutoScene` | 场景 |
| 演示 | `showDemoReport` / `initDemoReportScene` / `initDemo` / `restartDemo` / `leaveDemo` | 流程 |
| 生命周期 | `handlePageHide` | 清理 |
| 启动 | `bindEvents` / `init` / `runLayoutDebug` | 绑定/启动/调试 |

---

## 7.16 常见坑

| 现象 | 原因 | 修法 |
| --- | --- | --- |
| 页面白屏 | CDN SDK 加载失败 | 演示模式应仍可用；检查 `loadLiveKit` 错误分支 |
| 听不到考官 | 自动播放被拦截 | 点横幅“播放”；`room.startAudio()` |
| 转写重复成两行 | segment id 不稳定 | key 用 `identity|id`；partial 必须复用同一 id |
| 成绩单不弹 | data topic 不是 `interview` 或 JSON 坏 | 看控制台 warning；检查 `publish_event` |
| 上翻后不再滚动 | `autoScroll=false` | 点“回到最新” |
| 移动端横向溢出 | 固定宽度/长文本 | 用 390/360 宽度检查；`min-width:0`、`overflow-wrap` |
| 退出后还有声音 | 音频元素未清理 | `cleanupAudio()` |

---

## 7.17 检查点

1. 为什么转写用 `Map` 按 segment id 更新，而不是每来一个 partial 就 append？
2. `setPhase` 的 `silent` 参数解决什么问题？
3. `normalizeReport` 在后端已经规范化后，为什么还要做一遍？
4. 演示模式为什么不能替代真实 STT/TTS 测试？
5. 如果后端新增一个 `type: "hint"` 事件，旧前端会怎样？

下一章：如何用测试和调试工具验证这些行为。
