/* ==========================================================================
   考研复试 · AI 模拟面试工作台 — frontend/app.js
   Plain ES2019+ browser JavaScript. No build step, no dependencies other
   than the LiveKit client UMD bundle loaded before this file.

   Data channel contract (topic = "interview", UTF-8 JSON):
     { type: "status", state: "thinking" | "listening" | "speaking" }
     { type: "phase",  phase: "self_intro" | "pro_qa" | "compre_qa" | "report" | "completed" }
     { type: "report", report: { total_score, scores: {...}, core_weakness,
                                 improvement_tips, closing }, text?: string }

   Transcription arrives via RoomEvent.TranscriptionReceived. Everything the
   backend sends is treated as untrusted and rendered with textContent only.
   ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ *
   * Environment / constants
   * ------------------------------------------------------------------ */

  const LIVEKIT_CDN = 'https://cdn.jsdelivr.net/npm/livekit-client@2.11.0/dist/livekit-client.umd.min.js';

  let LK = window.LivekitClient || null;
  let LK_EVENTS = LK && LK.RoomEvent ? LK.RoomEvent : {};
  let liveKitPromise = null;

  const params = new URLSearchParams(window.location.search);
  const IS_DEMO = params.get('demo') === '1';
  const DEMO_SCENE = IS_DEMO ? (params.get('scene') || 'auto') : null;

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
  const AI_STATUS_TEXT = {
    idle: '等待接入',
    connecting: '正在接入考场',
    waiting: '等待考官接入',
    listening: '正在聆听你的作答',
    thinking: 'AI 思考中',
    speaking: '考官正在提问',
    offline: '已离线'
  };
  const CONN_LABEL = {
    idle: '未连接',
    connecting: '连接中',
    connected: '面试进行中',
    reconnecting: '重连中',
    ended: '面试结束',
    error: '连接失败'
  };
  const QUALITY_LABEL = { 0: '未知', 1: '差', 2: '一般', 3: '良好', 4: '优秀' };
  const MODE_LABELS = {
    quick: { name: '简单模式', hint: '题量小 · 节奏快' },
    real: { name: '实战模式', hint: '多题 · 多次追问' }
  };
  const RING_RADIUS = 52;
  const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

  const DEMO_CANDIDATE_ID = 'candidate-demo';
  const DEMO_EXAMINER_ID = 'examiner-ai';

  let interviewMode = params.get('mode') === 'real' ? 'real' : 'quick';

  const DEMO_EXAM_1 =
    '同学你好，欢迎参加本次考研复试模拟面试。请先用一分钟左右做一个自我介绍，重点谈谈你的本科专业、科研经历和报考动机。';
  const DEMO_CAND_1 =
    '各位老师好，我叫李明，本科就读于计算机科学与技术专业，目前绩点 3.8/4.0。本科期间我参与过国家级大学生创新训练项目，负责数据清洗与模型对比实验，也在实验室做过多模态检索的预研；报考贵校是因为课题组的方向与我的兴趣高度契合。';
  const DEMO_EXAM_2 =
    '好的，谢谢你的介绍。接下来进入专业问答环节：请谈谈你对大模型“幻觉”问题的理解，以及目前有哪些主流的缓解手段？';
  const DEMO_CAND_2_PARTIAL =
    '我认为大模型幻觉主要是指模型生成了看似合理但与事实不符的内容。从成因上看，一方面是训练数据本身存在噪声，另一方面是自回归生成机制缺少事实校验……';
  const DEMO_CAND_2_FULL =
    DEMO_CAND_2_PARTIAL +
    '在缓解手段上，常见的有检索增强生成、引入外部工具做事实核验，以及通过偏好对齐让模型学会在不确定时主动表达不确定性。我在项目中做过一个简单的 RAG 原型，把检索结果作为上下文后，事实性错误有明显下降。';
  const DEMO_COMPRE_Q =
    '最后一个环节是综合素质。如果课题组的任务与你的个人安排发生了冲突，你会怎么处理？';
  const DEMO_COMPRE_A =
    '我会先确认任务的优先级和截止时间，把冲突的具体信息同步给导师，而不是自己硬扛。然后提出可执行的调整方案，比如调整自己的时间安排、拆分任务或者寻求同门协作。过程中保持透明沟通，结束后复盘，尽量让同类冲突不再出现。';
  const DEMO_REPORT = {
    total_score: 86,
    scores: { profession_know: 88, exp_logic: 84, comper_quality: 87 },
    core_weakness:
      '专业概念的边界把握还不够精确：在追问“对比学习与自监督学习”时出现概念混用，回答缺少准确定义支撑。另外，答题时“结论先行”的结构还不稳定，部分回答先展开细节、最后才给出判断。',
    improvement_tips:
      '1. 回答专业问题前先用一句话给出结论，再展开 2–3 个支撑点，每个点配一个例子或对比。\n2. 针对简历里的科研经历，准备“背景—方法—结果—反思”四段式表述，方便追问时快速定位。\n3. 遇到不确定的问题，先界定概念范围再作答，避免直接套用相近术语。',
    closing:
      '整体表达自然、节奏稳定，专业基础与临场应变都达到了复试要求。把“结论先行”练成习惯，你会更有把握。'
  };

  /* 实战模式（real）演示脚本：多题 + 多次追问 */

  const DEMO_FU_Q1 =
    '好的。你提到参与过创新训练项目，能具体说说你在其中承担的核心工作，以及遇到的最大困难吗？';
  const DEMO_FU_A1 =
    '我主要负责数据清洗和模型对比实验的流程搭建。最大的困难是标注数据噪声比较大，不同标注员的标准不一致。我的做法是先抽样做一致性分析，再和团队把标注规范细化成判定清单并复核，最终把噪声率降到了可接受的范围。';
  const DEMO_FU_Q2 =
    '你刚才提到 RAG。如果检索回来的资料本身和问题不相关，你会怎么处理？';
  const DEMO_FU_A2 =
    '我会先把检索质量拆成召回和排序两个环节来看。召回层面可以调整切分粒度、做多路召回；排序层面可以加一个轻量的相关性过滤，或者在生成前先判断资料是否足够，不够就明确说明无法回答，而不是硬编。';
  const DEMO_PRO_Q2 =
    '第二个专业问题：请解释过拟合的常见成因，以及在深度学习中你会用哪些手段缓解？';
  const DEMO_PRO_A2 =
    '过拟合通常是模型容量相对数据量过大、训练轮数过多，或者数据分布单一造成的。常见手段有数据增强、权重衰减和 dropout 等正则化、早停，以及用交叉验证选超参；数据有限时也可以考虑迁移学习或适当简化模型结构。';
  const DEMO_FU_Q3 =
    '如果导师交给你的任务超出了你当前的能力范围，你会怎么推进？';
  const DEMO_FU_A3 =
    '我会先拆解任务，明确自己卡在哪一步，然后把问题描述清楚去请教导师或同门，而不是闷头拖延。同时会给出一个初步方案和时间节点，让对方知道我的进度和需要的支持。';
  const DEMO_COMPRE_Q2 =
    '最后一个问题：你如何看待科研中的失败？';
  const DEMO_COMPRE_A2 =
    '我觉得失败是常态，关键是能不能从失败里提取信息。我会记录实验失败的原因，区分是假设问题、实现问题还是数据问题，然后设计下一轮更小的验证，避免在同一个地方重复投入。';

  const DEMO_MODE_SCRIPT = {
    quick: [
      { phase: 'self_intro', examiner: DEMO_EXAM_1, candidate: DEMO_CAND_1 },
      { phase: 'pro_qa', examiner: DEMO_EXAM_2, candidate: DEMO_CAND_2_FULL },
      { phase: 'compre_qa', examiner: DEMO_COMPRE_Q, candidate: DEMO_COMPRE_A }
    ],
    real: [
      {
        phase: 'self_intro',
        examiner: DEMO_EXAM_1,
        candidate: DEMO_CAND_1,
        followUp: DEMO_FU_Q1,
        followUpAnswer: DEMO_FU_A1
      },
      {
        phase: 'pro_qa',
        examiner: DEMO_EXAM_2,
        candidate: DEMO_CAND_2_FULL,
        followUp: DEMO_FU_Q2,
        followUpAnswer: DEMO_FU_A2
      },
      { phase: 'pro_qa', examiner: DEMO_PRO_Q2, candidate: DEMO_PRO_A2 },
      {
        phase: 'compre_qa',
        examiner: DEMO_COMPRE_Q,
        candidate: DEMO_COMPRE_A,
        followUp: DEMO_FU_Q3,
        followUpAnswer: DEMO_FU_A3
      },
      { phase: 'compre_qa', examiner: DEMO_COMPRE_Q2, candidate: DEMO_COMPRE_A2 }
    ]
  };

  /* ------------------------------------------------------------------ *
   * DOM references
   * ------------------------------------------------------------------ */

  function byId(id) {
    return document.getElementById(id);
  }

  const body = document.body;
  const demoBadge = byId('demoBadge');
  const quality = byId('quality');
  const qualityText = byId('qualityText');
  const sessionTimer = byId('sessionTimer');
  const timerText = byId('timerText');
  const statusBadge = byId('statusBadge');
  const statusText = byId('statusText');

  const phaseChip = byId('phaseChip');
  const modeRow = byId('modeRow');
  const modeQuick = byId('modeQuick');
  const modeReal = byId('modeReal');
  const modeBadge = byId('modeBadge');
  const modeBadgeText = byId('modeBadgeText');
  const demoEmptyLink = byId('demoEmptyLink');
  const stepper = byId('stepper');
  const stepperFill = byId('stepperFill');
  const stepNodes = stepper ? Array.prototype.slice.call(stepper.querySelectorAll('.step')) : [];

  const aiTile = byId('aiTile');
  const aiStateText = byId('aiStateText');
  const userTile = byId('userTile');
  const userState = byId('userState');
  const micToggle = byId('micToggle');
  const micToggleText = byId('micToggleText');
  const stageHintText = byId('stageHintText');
  const demoLink = byId('demoLink');

  const transcriptPanel = byId('transcriptPanel');
  const liveTag = byId('liveTag');
  const transcriptCount = byId('transcriptCount');
  const transcriptScroll = byId('transcriptScroll');
  const transcriptEl = byId('transcript');
  const transcriptEmpty = byId('transcriptEmpty');
  const jumpLatest = byId('jumpLatest');
  const errorCard = byId('errorCard');
  const errorTitle = byId('errorTitle');
  const errorMsg = byId('errorMsg');
  const errorRetry = byId('errorRetry');

  const micBtn = byId('micBtn');
  const btnConnect = byId('btnConnect');
  const btnConnectLabel = byId('btnConnectLabel');
  const btnReport = byId('btnReport');
  const btnLeave = byId('btnLeave');
  const controlHint = byId('controlHint');

  const playbackBanner = byId('playbackBanner');
  const playbackBtn = byId('playbackBtn');

  const reportLayer = byId('reportLayer');
  const reportClose = byId('reportClose');
  const reportCloseBtn = byId('reportCloseBtn');
  const reportAgain = byId('reportAgain');
  const reportHero = reportLayer ? reportLayer.querySelector('.report-hero') : null;
  const reportDims = byId('reportDims');
  const reportBlocks = byId('reportBlocks');
  const reportRaw = byId('reportRaw');
  const reportScroll = byId('reportScroll');
  const reportGrade = byId('reportGrade');
  const reportSummary = byId('reportSummary');
  const scoreValue = byId('scoreValue');
  const ringFill = byId('ringFill');
  const reportWeakness = byId('reportWeakness');
  const reportTips = byId('reportTips');
  const reportClosing = byId('reportClosing');

  const toastRegion = byId('toastRegion');
  const audioSink = document.createElement('div');

  audioSink.className = 'audio-sink';
  audioSink.setAttribute('aria-hidden', 'true');
  audioSink.style.display = 'none';
  document.body.appendChild(audioSink);

  /* ------------------------------------------------------------------ *
   * Runtime state
   * ------------------------------------------------------------------ */

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

  const segments = new Map();
  const audioEls = new Map();
  const demoTimers = [];
  let demoSegId = 0;
  let demoPartialKey = null;
  let retryAction = null;
  let lastFocus = null;
  let autoScroll = true;
  let scrollQueued = false;
  let toastSeq = 0;

  const textDecoder = typeof window.TextDecoder === 'function' ? new window.TextDecoder('utf-8') : null;

  /* ------------------------------------------------------------------ *
   * Small utilities
   * ------------------------------------------------------------------ */

  function setText(node, text) {
    if (!node) return;
    const next = text == null ? '' : String(text);
    if (node.textContent !== next) node.textContent = next;
  }

  function toggle(node, visible) {
    if (node) node.hidden = !visible;
  }

  function clamp(value, min, max) {
    const number = Number(value);
    if (!Number.isFinite(number)) return min;
    return Math.min(max, Math.max(min, number));
  }

  function prefersReducedMotion() {
    return typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  }

  function createIcon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'ico');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  function clockNow() {
    const now = new Date();
    const parts = [now.getHours(), now.getMinutes(), now.getSeconds()].map(function (n) {
      return String(n).padStart(2, '0');
    });
    return parts.join(':');
  }

  function formatDuration(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');
  }

  /* ------------------------------------------------------------------ *
   * Toasts / inline errors
   * ------------------------------------------------------------------ */

  const TOAST_ICON = { info: 'i-info', success: 'i-check', warn: 'i-warning', error: 'i-warning' };

  function showToast(message, variant) {
    if (!toastRegion) return;
    const type = TOAST_ICON[variant] ? variant : 'info';
    while (toastRegion.children.length >= 3) {
      toastRegion.removeChild(toastRegion.firstChild);
    }
    const toast = document.createElement('div');
    toast.className = 'toast toast--' + type;
    toast.setAttribute('data-toast', String(++toastSeq));
    toast.appendChild(createIcon(TOAST_ICON[type]));
    const text = document.createElement('span');
    text.textContent = String(message == null ? '' : message);
    toast.appendChild(text);
    toastRegion.appendChild(toast);
    window.setTimeout(function () {
      toast.classList.add('is-leaving');
      window.setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 260);
    }, 4200);
  }

  function showError(title, message, action) {
    retryAction = typeof action === 'function' ? action : null;
    setText(errorTitle, title || '出错了');
    setText(errorMsg, message || '');
    toggle(errorCard, true);
    toggle(errorRetry, Boolean(retryAction));
  }

  function hideError() {
    retryAction = null;
    toggle(errorCard, false);
  }

  function describeMediaError(error) {
    const name = error && error.name ? String(error.name) : '';
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return '浏览器拒绝了麦克风权限。请在地址栏的站点权限中允许麦克风后重试。';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return '未检测到可用的麦克风设备，请连接麦克风后重试。';
    }
    if (name === 'NotReadableError' || name === 'TrackStartError') {
      return '麦克风被其他程序占用或无法读取，请关闭占用程序后重试。';
    }
    return (error && error.message) ? String(error.message) : '未知错误，请重试。';
  }

  /* ------------------------------------------------------------------ *
   * Connection badge / hints / controls
   * ------------------------------------------------------------------ */

  function setConn(conn) {
    state.conn = conn;
    body.dataset.conn = conn;
    if (statusBadge) {
      statusBadge.dataset.state = conn;
      statusBadge.setAttribute('role', 'status');
      statusBadge.setAttribute('aria-live', 'polite');
    }
    setText(statusText, CONN_LABEL[conn] || '未连接');
    toggle(liveTag, conn === 'connected');
    toggle(transcriptEmpty, !IS_DEMO && conn === 'idle');
    if (conn !== 'connected' && conn !== 'reconnecting') toggle(quality, false);
    updateHints();
    syncControls();
  }

  function updateHints() {
    const conn = state.conn;
    if (conn === 'connected') {
      if (IS_DEMO) {
        setText(stageHintText, '演示模式：模拟转写、阶段推进与成绩单均由本地脚本生成，不会连接 LiveKit。');
        setText(controlHint, '演示模式 · 本地模拟，不产生真实音频');
      } else {
        setText(stageHintText, '面试进行中：请自然作答，右侧会实时显示你与考官的转写内容。');
        setText(controlHint, '面试进行中 · 请允许浏览器播放考官声音');
      }
    } else if (conn === 'connecting') {
      setText(stageHintText, '正在申请麦克风权限并接入考场，请在浏览器弹窗中允许使用麦克风…');
      setText(controlHint, '正在接入考场…');
    } else if (conn === 'reconnecting') {
      setText(stageHintText, '网络出现波动，正在自动重连，请稍候…');
      setText(controlHint, '网络波动 · 正在重连…');
    } else if (conn === 'error') {
      setText(stageHintText, '接入失败，请点击「重试」或检查后端服务与网络状态。');
      setText(controlHint, '连接失败 · 请重试');
    } else if (conn === 'ended') {
      setText(stageHintText, '面试已结束，可查看成绩单或重新进入考场。');
      setText(controlHint, '面试已结束 · 可查看成绩单或重新开始');
    } else {
      setText(stageHintText, '进入考场后，AI 考官会通过语音与你完成一场完整模拟面试；请允许麦克风权限。');
      setText(controlHint, '准备就绪 · 进入考场后将自动申请麦克风权限');
    }
    toggle(demoLink, !IS_DEMO && conn === 'idle');
  }

  function modeIsLocked() {
    return state.starting
      || state.conn === 'connected'
      || state.conn === 'connecting'
      || state.conn === 'reconnecting';
  }

  function syncModeUI() {
    const quickActive = interviewMode === 'quick';
    if (modeQuick) {
      modeQuick.classList.toggle('is-active', quickActive);
      modeQuick.setAttribute('aria-pressed', String(quickActive));
    }
    if (modeReal) {
      modeReal.classList.toggle('is-active', !quickActive);
      modeReal.setAttribute('aria-pressed', String(!quickActive));
    }
    setText(modeBadgeText, MODE_LABELS[interviewMode] ? MODE_LABELS[interviewMode].name : '简单模式');
    const demoHref = '?demo=1&mode=' + interviewMode;
    if (demoLink) demoLink.setAttribute('href', demoHref);
    if (demoEmptyLink) demoEmptyLink.setAttribute('href', demoHref);
  }

  function setMode(mode) {
    interviewMode = mode === 'real' ? 'real' : 'quick';
    syncModeUI();
  }

  function selectMode(mode) {
    if (modeIsLocked()) {
      showToast('面试进行中不可切换模式，请先退出考场。', 'warn');
      return;
    }
    if (interviewMode === mode) return;
    setMode(mode);
    showToast('已切换到' + MODE_LABELS[interviewMode].name + '（' + MODE_LABELS[interviewMode].hint + '）', 'info');
  }

  function syncControls() {
    const conn = state.conn;
    const connecting = state.starting || conn === 'connecting';
    const connected = conn === 'connected';
    const reconnecting = conn === 'reconnecting';
    const ended = conn === 'ended';

    if (btnConnect) {
      btnConnect.disabled = connecting || connected || reconnecting;
      if (connecting) setText(btnConnectLabel, '正在连接…');
      else if (connected) setText(btnConnectLabel, IS_DEMO ? '演示进行中' : '面试进行中');
      else if (ended || conn === 'error') setText(btnConnectLabel, IS_DEMO ? '再练一次' : '重新进入');
      else setText(btnConnectLabel, '进入考场');
    }
    if (btnLeave) btnLeave.disabled = !(connected || reconnecting);
    if (btnReport) {
      btnReport.disabled = !state.report;
      btnReport.title = state.report ? '查看本场成绩单' : '本场面试结束后可查看成绩单';
    }
    const micAvailable = connected;
    if (micBtn) micBtn.disabled = !micAvailable;
    if (micToggle) micToggle.disabled = !micAvailable;

    const locked = modeIsLocked();
    if (modeRow) modeRow.hidden = locked;
    if (modeBadge) modeBadge.hidden = !locked;
    [modeQuick, modeReal].forEach(function (button) {
      if (button) button.disabled = locked;
    });
  }

  function syncMicUI() {
    const active = state.conn === 'connected';
    const muted = !state.micEnabled;
    [micBtn, micToggle].forEach(function (button) {
      if (!button) return;
      button.classList.toggle('is-muted', active && muted);
      button.setAttribute('aria-pressed', String(active && muted));
      const label = !active ? '麦克风（未连接）' : (muted ? '开启麦克风' : '静音麦克风');
      button.setAttribute('aria-label', label);
    });
    setText(micToggleText, !active ? '麦克风' : (muted ? '已静音' : '麦克风'));
    if (userTile) userTile.dataset.mic = active && !muted ? 'on' : 'off';

    if (state.conn === 'connected') {
      setText(userState, muted ? '麦克风已静音' : '麦克风已开启 · 请自然作答');
    } else if (state.conn === 'ended') {
      setText(userState, '已退出考场');
    } else if (state.conn === 'connecting') {
      setText(userState, '等待接入…');
    } else {
      setText(userState, '未进入考场');
    }
  }

  /* ------------------------------------------------------------------ *
   * Phase stepper (driven by the data channel)
   * ------------------------------------------------------------------ */

  function setPhase(phase, silent) {
    if (phase !== null && !PHASE_LABELS[phase]) return;
    const previous = state.phase;
    state.phase = phase;
    const completed = phase === 'completed';
    const activeIndex = completed || !phase
      ? -1
      : PHASES.findIndex(function (item) { return item.key === phase; });

    stepNodes.forEach(function (node, index) {
      const done = completed || (activeIndex >= 0 && index < activeIndex);
      const active = activeIndex === index;
      node.classList.toggle('is-done', done);
      node.classList.toggle('is-active', active);
      if (active) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    });

    let progress = 0;
    if (completed) progress = 1;
    else if (activeIndex >= 0) progress = activeIndex / (PHASES.length - 1);
    if (stepperFill) stepperFill.style.setProperty('--progress', String(progress));

    if (!phase) setText(phaseChip, '未开始');
    else if (completed) setText(phaseChip, '面试已结束');
    else setText(phaseChip, '第 ' + (activeIndex + 1) + ' / ' + PHASES.length + ' 阶段 · ' + PHASE_LABELS[phase]);

    if (phase && phase !== previous && !silent) {
      if (phase === 'completed') logSystem('面试流程已完成。', 'success');
      else logSystem('进入「' + PHASE_LABELS[phase] + '」阶段。');
    }
  }

  /* ------------------------------------------------------------------ *
   * AI status / speaking indicators
   * ------------------------------------------------------------------ */

  function setAiStatus(status) {
    state.aiStatus = status;
    if (aiTile) aiTile.dataset.status = status;
    setText(aiStateText, AI_STATUS_TEXT[status] || AI_STATUS_TEXT.idle);
    if (aiTile) {
      aiTile.classList.toggle('is-thinking', status === 'thinking');
      aiTile.classList.toggle('is-speaking', status === 'speaking');
      aiTile.classList.toggle('is-listening', status === 'listening');
    }
  }

  function setTileSpeaking(role, speaking) {
    const tile = role === 'candidate' ? userTile : aiTile;
    if (tile) tile.classList.toggle('is-speaking', speaking);
  }

  function markCandidateFinished() {
    if (state.phase === 'report' || state.phase === 'completed') return;
    setAiStatus('thinking');
    if (state.thinkingTimer) window.clearTimeout(state.thinkingTimer);
    state.thinkingTimer = window.setTimeout(function () {
      state.thinkingTimer = null;
      if (state.aiStatus === 'thinking') setAiStatus('listening');
    }, 12000);
  }

  function markExaminerFinished() {
    if (state.thinkingTimer) {
      window.clearTimeout(state.thinkingTimer);
      state.thinkingTimer = null;
    }
    if (state.phase === 'report' || state.phase === 'completed') return;
    setAiStatus('listening');
  }

  function handleActiveSpeakers(speakers) {
    let aiSpeaking = false;
    let userSpeaking = false;
    (speakers || []).forEach(function (speaker) {
      if (!speaker) return;
      if (speaker.isLocal) userSpeaking = true;
      else aiSpeaking = true;
    });
    if (userTile) userTile.classList.toggle('is-speaking', userSpeaking);
    if (aiSpeaking) setAiStatus('speaking');
    else if (state.aiStatus === 'speaking') setAiStatus('listening');
  }

  function updateQuality(rawQuality) {
    const name = String(rawQuality == null ? '' : rawQuality).toLowerCase();
    let level = 0;
    if (name === 'excellent') level = 4;
    else if (name === 'good') level = 3;
    else if (name === 'poor') level = 2;
    else if (name === 'lost') level = 1;
    else {
      const numeric = Number(rawQuality);
      if (Number.isFinite(numeric) && numeric >= 0 && numeric <= 4) level = Math.round(numeric);
    }
    if (quality) {
      quality.dataset.level = String(level);
      quality.hidden = false;
      quality.setAttribute('aria-label', '连接质量：' + QUALITY_LABEL[level]);
    }
    setText(qualityText, QUALITY_LABEL[level]);
  }

  /* ------------------------------------------------------------------ *
   * Session timer
   * ------------------------------------------------------------------ */

  function startTimer() {
    stopTimer();
    state.timerStart = Date.now();
    setText(timerText, '00:00');
    toggle(sessionTimer, true);
    state.timerId = window.setInterval(function () {
      setText(timerText, formatDuration(Date.now() - state.timerStart));
    }, 1000);
  }

  function stopTimer() {
    if (state.timerId) {
      window.clearInterval(state.timerId);
      state.timerId = null;
    }
  }

  /* ------------------------------------------------------------------ *
   * Transcript rendering (textContent only — never innerHTML)
   * ------------------------------------------------------------------ */

  function updateTranscriptCount() {
    const count = transcriptEl ? transcriptEl.childElementCount : 0;
    setText(transcriptCount, count + ' 条');
  }

  function scheduleScroll() {
    if (!autoScroll || !transcriptScroll || scrollQueued) return;
    scrollQueued = true;
    window.requestAnimationFrame(function () {
      scrollQueued = false;
      if (!autoScroll) return;
      transcriptScroll.scrollTop = transcriptScroll.scrollHeight;
    });
  }

  function appendTranscript(node) {
    if (!transcriptEl) return;
    transcriptEl.appendChild(node);
    updateTranscriptCount();
    scheduleScroll();
  }

  function createTranscriptLine(role) {
    const isCandidate = role === 'candidate';
    const wrap = document.createElement('div');
    wrap.className = 'tline tline--' + (isCandidate ? 'candidate' : 'examiner');

    const avatar = document.createElement('div');
    avatar.className = 'tline-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.appendChild(createIcon(isCandidate ? 'i-person' : 'i-cap'));

    const main = document.createElement('div');
    main.className = 'tline-main';

    const meta = document.createElement('div');
    meta.className = 'tline-meta';
    const name = document.createElement('span');
    name.className = 'tline-name';
    name.textContent = isCandidate ? '考生' : '考官';
    const tag = document.createElement('span');
    tag.className = 'tline-role-tag';
    tag.textContent = isCandidate ? '你' : 'AI';
    const time = document.createElement('time');
    time.className = 'tline-time';
    time.textContent = clockNow();
    meta.appendChild(name);
    meta.appendChild(tag);
    meta.appendChild(time);

    const text = document.createElement('p');
    text.className = 'tline-text';

    main.appendChild(meta);
    main.appendChild(text);
    wrap.appendChild(avatar);
    wrap.appendChild(main);
    return { el: wrap, textEl: text };
  }

  function logSystem(message, variant) {
    const row = document.createElement('div');
    row.className = 'tsys' + (variant ? ' tsys--' + variant : '');
    row.appendChild(createIcon('i-info'));
    const paragraph = document.createElement('p');
    paragraph.textContent = String(message == null ? '' : message);
    row.appendChild(paragraph);
    appendTranscript(row);
    return row;
  }

  function upsertSegment(role, key, text, isFinal) {
    const clean = typeof text === 'string' ? text : '';
    const safeKey = key || (role + ':auto:' + (++demoSegId));
    let record = segments.get(safeKey);

    if (!record) {
      if (!clean) return null;
      record = createTranscriptLine(role);
      segments.set(safeKey, record);
      appendTranscript(record.el);
    }

    if (clean || !isFinal) setText(record.textEl, clean);

    if (isFinal) {
      if (!clean) {
        if (record.el.parentNode) record.el.parentNode.removeChild(record.el);
        segments.delete(safeKey);
        updateTranscriptCount();
        return null;
      }
      record.el.classList.remove('is-partial');
      record.el.classList.add('is-final');
    } else {
      record.el.classList.add('is-partial');
    }
    scheduleScroll();
    return record;
  }

  function getLocalIdentity() {
    if (state.room && state.room.localParticipant && state.room.localParticipant.identity) {
      return String(state.room.localParticipant.identity);
    }
    return state.localIdentity || '';
  }

  function handleTranscription(incomingSegments, participant) {
    if (!Array.isArray(incomingSegments)) return;
    const localIdentity = getLocalIdentity();
    const identity = participant && participant.identity ? String(participant.identity) : '';
    const isCandidate = Boolean(identity) && Boolean(localIdentity) && identity === localIdentity;
    const role = isCandidate ? 'candidate' : 'examiner';

    incomingSegments.forEach(function (segment, index) {
      if (!segment || typeof segment.text !== 'string') return;
      const rawId = segment.id !== undefined && segment.id !== null ? String(segment.id) : 'idx-' + index;
      const key = identity + '|' + rawId;
      upsertSegment(role, key, segment.text, Boolean(segment.final));
      if (segment.final) {
        if (role === 'candidate') markCandidateFinished();
        else markExaminerFinished();
      }
    });
  }

  function handleTranscriptScroll() {
    if (!transcriptScroll) return;
    const distance = transcriptScroll.scrollHeight - transcriptScroll.scrollTop - transcriptScroll.clientHeight;
    const atBottom = distance < 56;
    if (atBottom !== autoScroll) {
      autoScroll = atBottom;
      toggle(jumpLatest, !autoScroll);
    }
  }

  /* ------------------------------------------------------------------ *
   * Data channel (tolerant decoding)
   * ------------------------------------------------------------------ */

  function decodePayload(payload) {
    if (typeof payload === 'string') return payload;
    if (textDecoder && payload) {
      if (payload instanceof Uint8Array || payload instanceof ArrayBuffer) return textDecoder.decode(payload);
    }
    if (payload && payload.byteLength !== undefined) {
      return new window.TextDecoder('utf-8').decode(new Uint8Array(payload));
    }
    return '';
  }

  function handleDataPacket(payload, topic) {
    if (topic && String(topic) !== 'interview') return;
    if (!payload) return;
    let raw = '';
    try {
      raw = decodePayload(payload);
    } catch (error) {
      console.warn('[interview] 数据包解码失败', error);
      return;
    }
    if (!raw) return;
    let message = null;
    try {
      message = JSON.parse(raw);
    } catch (error) {
      console.warn('[interview] 数据消息不是合法 JSON，已忽略');
      return;
    }
    handleDataMessage(message);
  }

  function handleDataMessage(message) {
    if (!message || typeof message !== 'object') return;
    const type = String(message.type || '').toLowerCase();

    if (type === 'status') {
      const next = String(message.state || '').toLowerCase();
      if (next === 'thinking' || next === 'listening' || next === 'speaking') {
        if (next !== 'thinking' && state.thinkingTimer) {
          window.clearTimeout(state.thinkingTimer);
          state.thinkingTimer = null;
        }
        setAiStatus(next);
      }
      return;
    }

    if (type === 'phase') {
      const next = String(message.phase || '').toLowerCase();
      if (next === 'completed' || PHASES.some(function (item) { return item.key === next; })) {
        setPhase(next);
      }
      return;
    }

    if (type === 'report') {
      handleReportMessage(message);
      return;
    }

    // Unknown message types are ignored on purpose (forward compatibility).
  }

  /* ------------------------------------------------------------------ *
   * Report panel
   * ------------------------------------------------------------------ */

  function toScore(value) {
    const number = Number(value);
    return Number.isFinite(number) ? clamp(number, 0, 100) : 0;
  }

  function normalizeReport(raw) {
    if (!raw || typeof raw !== 'object') return null;
    const scores = raw.scores && typeof raw.scores === 'object' ? raw.scores : null;
    const totalRaw = Number(raw.total_score);
    if (!scores && !Number.isFinite(totalRaw)) return null;
    const dims = {
      profession_know: toScore(scores ? scores.profession_know : 0),
      exp_logic: toScore(scores ? scores.exp_logic : 0),
      comper_quality: toScore(scores ? scores.comper_quality : 0)
    };
    const average = (dims.profession_know + dims.exp_logic + dims.comper_quality) / 3;
    const total = Number.isFinite(totalRaw) ? clamp(totalRaw, 0, 100) : Math.round(average);
    return {
      total: total,
      dims: dims,
      weakness: typeof raw.core_weakness === 'string' ? raw.core_weakness.trim() : '',
      tips: typeof raw.improvement_tips === 'string' ? raw.improvement_tips.trim() : '',
      closing: typeof raw.closing === 'string' ? raw.closing.trim() : ''
    };
  }

  function gradeFor(total) {
    if (total >= 85) return { label: '优秀', level: 'excellent' };
    if (total >= 75) return { label: '良好', level: 'good' };
    if (total >= 60) return { label: '合格', level: 'pass' };
    return { label: '待提升', level: 'weak' };
  }

  function roundScore(value) {
    return String(Math.round(clamp(value, 0, 100)));
  }

  function animateNumber(node, target, duration) {
    if (!node) return;
    const to = Math.round(clamp(target, 0, 100));
    if (!duration || duration <= 0 || prefersReducedMotion() || !window.requestAnimationFrame) {
      setText(node, to);
      return;
    }
    const start = window.performance && window.performance.now ? window.performance.now() : Date.now();
    const from = 0;
    function frame(now) {
      const elapsed = now - start;
      const t = Math.min(1, elapsed / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setText(node, Math.round(from + (to - from) * eased));
      if (t < 1) window.requestAnimationFrame(frame);
      else setText(node, to);
    }
    window.requestAnimationFrame(frame);
  }

  function resetRingVisual() {
    if (!ringFill) return;
    ringFill.style.transition = 'none';
    ringFill.style.strokeDasharray = RING_CIRCUMFERENCE.toFixed(2);
    ringFill.style.strokeDashoffset = RING_CIRCUMFERENCE.toFixed(2);
    if (ringFill.getBoundingClientRect) ringFill.getBoundingClientRect();
    ringFill.style.transition = '';
  }

  function animateRing(total) {
    if (!ringFill) return;
    const pct = clamp(total, 0, 100) / 100;
    ringFill.style.transition = 'none';
    ringFill.style.strokeDasharray = RING_CIRCUMFERENCE.toFixed(2);
    ringFill.style.strokeDashoffset = RING_CIRCUMFERENCE.toFixed(2);
    if (ringFill.getBoundingClientRect) ringFill.getBoundingClientRect();
    ringFill.style.transition = '';
    window.requestAnimationFrame(function () {
      ringFill.style.strokeDashoffset = (RING_CIRCUMFERENCE * (1 - pct)).toFixed(2);
    });
  }

  function setReportSectionsVisible(mode) {
    const structured = mode === 'structured';
    toggle(reportHero, structured);
    toggle(reportDims, structured);
    toggle(reportBlocks, structured);
    toggle(reportRaw, !structured);
  }

  function applyStructuredReport(data) {
    if (!data) return;
    setReportSectionsVisible('structured');
    const grade = gradeFor(data.total);
    setText(reportGrade, grade.label);
    if (reportGrade) reportGrade.dataset.level = grade.level;
    setText(reportSummary, '本场面试已完成 · AI 考官依据对话转写与作答表现给出综合评估。');
    animateNumber(scoreValue, data.total, IS_DEMO ? 0 : 900);
    animateRing(data.total);

    DIM_KEYS.forEach(function (key, index) {
      const score = clamp(data.dims[key], 0, 100);
      setText(byId('dimValue-' + key), roundScore(score));
      const fill = byId('dimFill-' + key);
      if (fill) {
        fill.classList.remove('is-in');
        fill.style.setProperty('--pct', (score / 100).toFixed(3));
        fill.style.transitionDelay = (index * 90) + 'ms';
        window.requestAnimationFrame(function () {
          fill.classList.add('is-in');
        });
      }
    });

    setText(reportWeakness, data.weakness || '暂无');
    setText(reportTips, data.tips || '暂无');
    setText(reportClosing, data.closing || '');
  }

  function applyRawReport(text) {
    setReportSectionsVisible('raw');
    setText(reportRaw, text || '');
  }

  function resetReportUI() {
    setReportSectionsVisible('structured');
    setText(scoreValue, '--');
    setText(reportGrade, '待评估');
    if (reportGrade) reportGrade.removeAttribute('data-level');
    setText(reportSummary, '本场面试已完成，以下为 AI 考官依据对话内容给出的综合评估。');
    setText(reportWeakness, '');
    setText(reportTips, '');
    setText(reportClosing, '');
    setText(reportRaw, '');
    DIM_KEYS.forEach(function (key) {
      setText(byId('dimValue-' + key), '--');
      const fill = byId('dimFill-' + key);
      if (fill) {
        fill.classList.remove('is-in');
        fill.style.setProperty('--pct', '0');
        fill.style.transitionDelay = '';
      }
    });
    resetRingVisual();
  }

  function handleReportMessage(message) {
    const data = normalizeReport(message.report);
    const rawText = typeof message.text === 'string' ? message.text.trim() : '';

    if (data) {
      state.report = { kind: 'structured', data: data };
    } else if (rawText) {
      state.report = { kind: 'raw', text: rawText };
    } else {
      showToast('收到成绩单消息，但内容为空或格式异常。', 'warn');
      return;
    }

    setPhase(state.phase === 'completed' ? 'completed' : 'report');
    setAiStatus('offline');
    toggle(liveTag, false);
    openReport();
    showToast('本场成绩单已生成', 'success');
    syncControls();
  }

  function openReport() {
    if (!state.report) {
      showToast('本场面试尚未生成成绩单。', 'info');
      return;
    }
    if (state.report.kind === 'structured') applyStructuredReport(state.report.data);
    else applyRawReport(state.report.text);

    if (!reportLayer) return;
    lastFocus = document.activeElement;
    reportLayer.hidden = false;
    reportLayer.setAttribute('aria-hidden', 'false');
    body.classList.add('modal-open');
    window.requestAnimationFrame(function () {
      reportLayer.classList.add('is-open');
    });
    window.requestAnimationFrame(function () {
      if (reportClose) {
        try { reportClose.focus({ preventScroll: true }); } catch (error) { reportClose.focus(); }
      }
    });
    if (reportScroll) reportScroll.scrollTop = 0;
  }

  function closeReport(immediate) {
    if (!reportLayer || reportLayer.hidden) return;
    reportLayer.classList.remove('is-open');
    body.classList.remove('modal-open');
    const finish = function () {
      reportLayer.hidden = true;
      reportLayer.setAttribute('aria-hidden', 'true');
    };
    if (immediate || prefersReducedMotion()) finish();
    else window.setTimeout(finish, 300);
    if (lastFocus && typeof lastFocus.focus === 'function') {
      try { lastFocus.focus({ preventScroll: true }); } catch (error) { lastFocus.focus(); }
    }
    lastFocus = null;
  }

  function handleReportKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeReport();
      return;
    }
    if (event.key !== 'Tab' || !reportLayer) return;
    const focusable = Array.prototype.slice.call(
      reportLayer.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    ).filter(function (node) {
      return !node.disabled && node.offsetParent !== null;
    });
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /* ------------------------------------------------------------------ *
   * Audio playback / autoplay handling
   * ------------------------------------------------------------------ */

  function isAudioTrack(track) {
    if (!track) return false;
    if (track.kind === 'audio') return true;
    const audioKind = LK && LK.Track && LK.Track.Kind ? LK.Track.Kind.Audio : null;
    return Boolean(audioKind) && track.kind === audioKind;
  }

  function attachAudioTrack(track) {
    if (!track) return;
    const sid = track.sid
      || (track.mediaStreamTrack && track.mediaStreamTrack.id)
      || ('audio-' + Date.now() + '-' + Math.random().toString(16).slice(2));
    if (audioEls.has(sid)) return;
    let element = null;
    try {
      element = track.attach();
    } catch (error) {
      console.warn('[interview] 音频挂载失败', error);
      return;
    }
    if (!element) return;
    element.autoplay = true;
    element.setAttribute('playsinline', '');
    element.dataset.trackSid = sid;
    audioSink.appendChild(element);
    audioEls.set(sid, element);
    const promise = element.play();
    if (promise && typeof promise.catch === 'function') {
      promise.catch(function () {
        if (state.conn === 'connected') showPlaybackPrompt(true);
      });
    }
    logSystem('已接通考官音频通道。', 'success');
  }

  function detachAudioTrack(track) {
    if (track) {
      const sid = track.sid;
      if (sid && audioEls.has(sid)) {
        const element = audioEls.get(sid);
        try { element.pause(); } catch (error) { /* noop */ }
        if (element.parentNode) element.parentNode.removeChild(element);
        audioEls.delete(sid);
      }
      if (typeof track.detach === 'function') {
        try {
          track.detach().forEach(function (node) {
            if (node && node.parentNode) node.parentNode.removeChild(node);
          });
        } catch (error) { /* noop */ }
      }
    }
  }

  function cleanupAudio() {
    audioEls.forEach(function (element) {
      try { element.pause(); } catch (error) { /* noop */ }
      if (element.parentNode) element.parentNode.removeChild(element);
    });
    audioEls.clear();
    if (audioSink) audioSink.replaceChildren();
  }

  function showPlaybackPrompt(visible) {
    toggle(playbackBanner, Boolean(visible));
  }

  /* ------------------------------------------------------------------ *
   * Room wiring
   * ------------------------------------------------------------------ */

  function onRoomEvent(room, eventName, handler) {
    if (!eventName) return;
    try {
      room.on(eventName, handler);
    } catch (error) {
      console.warn('[interview] 无法监听会话事件', eventName, error);
    }
  }

  function handleRoomDisconnected(room) {
    if (state.room !== room) return;
    if (state.starting) return;
    state.room = null;
    teardownSession();
    logSystem('与考场的连接已断开。', 'warn');
    showError('连接已断开', '与考场的连接已中断，可点击重试重新进入。', startInterview);
  }

  function wireRoom(room) {
    onRoomEvent(room, LK_EVENTS.TrackSubscribed, function (track) {
      if (isAudioTrack(track)) attachAudioTrack(track);
    });
    onRoomEvent(room, LK_EVENTS.TrackUnsubscribed, function (track) {
      detachAudioTrack(track);
    });
    onRoomEvent(room, LK_EVENTS.ActiveSpeakersChanged, handleActiveSpeakers);
    onRoomEvent(room, LK_EVENTS.AudioPlaybackStatusChanged, function () {
      if (state.room !== room) return;
      if (state.conn !== 'connected' && state.conn !== 'reconnecting') return;
      showPlaybackPrompt(!room.canPlaybackAudio);
    });
    onRoomEvent(room, LK_EVENTS.ConnectionQualityChanged, function (connectionQuality, participant) {
      if (!participant || participant.isLocal) updateQuality(connectionQuality);
    });
    onRoomEvent(room, LK_EVENTS.DataReceived, function (payload, participant, kind, topic) {
      handleDataPacket(payload, topic);
    });
    onRoomEvent(room, LK_EVENTS.TranscriptionReceived, function (incoming, participant) {
      handleTranscription(incoming, participant);
    });
    onRoomEvent(room, LK_EVENTS.ParticipantConnected, function (participant) {
      const identity = participant && participant.identity ? participant.identity : '考官';
      logSystem('参与者「' + identity + '」已加入考场。', 'success');
    });
    onRoomEvent(room, LK_EVENTS.ParticipantDisconnected, function (participant) {
      const identity = participant && participant.identity ? participant.identity : '未知';
      logSystem('参与者「' + identity + '」已离开考场。', 'warn');
    });
    onRoomEvent(room, LK_EVENTS.Reconnecting, function () {
      setConn('reconnecting');
      logSystem('网络波动，正在自动重连…', 'warn');
    });
    onRoomEvent(room, LK_EVENTS.Reconnected, function () {
      setConn('connected');
      logSystem('连接已恢复。', 'success');
    });
    onRoomEvent(room, LK_EVENTS.Disconnected, function () {
      handleRoomDisconnected(room);
    });
    onRoomEvent(room, LK_EVENTS.MediaDevicesError, function (error) {
      showError('麦克风不可用', describeMediaError(error), enableMic);
    });
  }

  /* ------------------------------------------------------------------ *
   * Connect / disconnect (real LiveKit session)
   * ------------------------------------------------------------------ */

  function loadLiveKit() {
    if (window.LivekitClient) {
      LK = window.LivekitClient;
      LK_EVENTS = LK.RoomEvent || {};
      return Promise.resolve(LK);
    }
    if (liveKitPromise) return liveKitPromise;
    liveKitPromise = new Promise(function (resolve, reject) {
      const script = document.createElement('script');
      script.src = LIVEKIT_CDN;
      script.async = true;
      script.crossOrigin = 'anonymous';
      script.onload = function () {
        if (window.LivekitClient) {
          LK = window.LivekitClient;
          LK_EVENTS = LK.RoomEvent || {};
          resolve(LK);
        } else {
          liveKitPromise = null;
          reject(new Error('SDK 未挂载全局 LivekitClient'));
        }
      };
      script.onerror = function () {
        liveKitPromise = null;
        reject(new Error('无法加载 LiveKit SDK'));
      };
      document.head.appendChild(script);
    });
    return liveKitPromise;
  }

  async function startInterview() {
    if (state.starting) return;
    if (IS_DEMO) {
      restartDemo();
      return;
    }
    hideError();
    showPlaybackPrompt(false);

    state.starting = true;
    state.report = null;
    resetReportUI();
    setPhase(null, true);
    setConn('connecting');
    setAiStatus('connecting');
    logSystem('正在加载实时通信组件…');
    syncControls();

    try {
      await loadLiveKit();
    } catch (error) {
      state.starting = false;
      setConn('error');
      setAiStatus('idle');
      logSystem('实时通信组件加载失败。', 'error');
      showError(
        '实时通信组件未加载',
        '无法从 CDN 加载 LiveKit SDK。请检查网络后重试；无后端时可用 ?demo=1 体验完整界面。',
        startInterview
      );
      syncControls();
      syncMicUI();
      return;
    }
    LK_EVENTS = LK && LK.RoomEvent ? LK.RoomEvent : {};

    logSystem('正在获取入场凭证（' + MODE_LABELS[interviewMode].name + '）…');

    let room = null;
    try {
      const response = await fetch(
        '/api/token?room=ai-room&mode=' + encodeURIComponent(interviewMode),
        {
          headers: { Accept: 'application/json' },
          cache: 'no-store'
        }
      );
      if (!response.ok) throw new Error('获取入场凭证失败（HTTP ' + response.status + '）');
      const connection = await response.json();
      if (!connection || !connection.token || !connection.serverUrl) {
        throw new Error('入场凭证格式不正确');
      }
      // 后端返回 interviewMode 时以服务端为准（字段缺失则保留本地选择）。
      if (connection.interviewMode === 'quick' || connection.interviewMode === 'real') {
        if (connection.interviewMode !== interviewMode) {
          setMode(connection.interviewMode);
          logSystem('服务端校准面试模式为' + MODE_LABELS[interviewMode].name + '。', 'warn');
        }
      }
      state.localIdentity = connection.identity ? String(connection.identity) : '';

      logSystem('正在连接考场' + (connection.roomName ? '「' + connection.roomName + '」' : '') + '…');
      room = new LK.Room({ adaptiveStream: true, dynacast: true });
      state.room = room;
      wireRoom(room);
      await room.connect(connection.serverUrl, connection.token);
      if (state.room !== room) return;
    } catch (error) {
      if (state.room === room) state.room = null;
      if (room) {
        try { room.disconnect(); } catch (disconnectError) { /* noop */ }
      }
      state.starting = false;
      setConn('error');
      setAiStatus('idle');
      logSystem('进入考场失败：' + describeMediaError(error), 'error');
      showError('无法进入考场', friendlyConnectionError(error), startInterview);
      syncControls();
      syncMicUI();
      return;
    }

    state.starting = false;
    setConn('connected');
    setAiStatus('waiting');
    startTimer();
    logSystem('已进入考场（' + MODE_LABELS[interviewMode].name + '），正在开启音频通路…', 'success');
    if (state.phase === null) setPhase('self_intro', true);

    try {
      await room.startAudio();
    } catch (error) {
      showPlaybackPrompt(true);
    }

    try {
      await room.localParticipant.setMicrophoneEnabled(true);
      state.micEnabled = true;
      setAiStatus('listening');
      logSystem('麦克风已开启，请自然作答。', 'success');
      hideError();
    } catch (error) {
      state.micEnabled = false;
      showError('麦克风开启失败', describeMediaError(error), enableMic);
      logSystem('麦克风开启失败：' + describeMediaError(error), 'error');
    }

    syncMicUI();
    syncControls();
  }

  function friendlyConnectionError(error) {
    const message = error && error.message ? String(error.message) : '';
    if (/Failed to fetch|NetworkError|Load failed/i.test(message)) {
      return '无法连接后端服务或 LiveKit 服务器，请确认服务已启动、网络可达后重试。';
    }
    return message || '连接过程中发生未知错误，请重试。';
  }

  async function enableMic() {
    const room = state.room;
    if (!room || state.conn !== 'connected') {
      showToast('尚未接入考场，无法开启麦克风。', 'warn');
      return;
    }
    try {
      await room.localParticipant.setMicrophoneEnabled(true);
      state.micEnabled = true;
      hideError();
      syncMicUI();
      logSystem('麦克风已开启。', 'success');
    } catch (error) {
      state.micEnabled = false;
      syncMicUI();
      showError('麦克风不可用', describeMediaError(error), enableMic);
    }
  }

  async function toggleMic() {
    if (IS_DEMO) {
      state.micEnabled = !state.micEnabled;
      syncMicUI();
      showToast(state.micEnabled ? '麦克风已开启' : '麦克风已静音', state.micEnabled ? 'success' : 'warn');
      return;
    }
    const room = state.room;
    if (!room || state.conn !== 'connected') return;
    const target = !state.micEnabled;
    try {
      await room.localParticipant.setMicrophoneEnabled(target);
      state.micEnabled = target;
      syncMicUI();
      showToast(target ? '麦克风已开启' : '麦克风已静音', target ? 'success' : 'warn');
    } catch (error) {
      showToast('麦克风切换失败：' + describeMediaError(error), 'error');
    }
  }

  async function restorePlayback() {
    const room = state.room;
    if (!room) {
      showPlaybackPrompt(false);
      return;
    }
    try {
      await room.startAudio();
      showPlaybackPrompt(!room.canPlaybackAudio);
      if (room.canPlaybackAudio) logSystem('音频播放已恢复。', 'success');
    } catch (error) {
      showToast('恢复音频播放失败：' + (error && error.message ? error.message : '未知错误'), 'error');
    }
  }

  function teardownSession() {
    clearDemoTimers();
    if (state.thinkingTimer) {
      window.clearTimeout(state.thinkingTimer);
      state.thinkingTimer = null;
    }
    stopTimer();
    cleanupAudio();
    showPlaybackPrompt(false);
    const room = state.room;
    state.room = null;
    if (room) {
      try {
        room.disconnect();
      } catch (error) {
        console.warn('[interview] 断开连接时出错', error);
      }
    }
    state.starting = false;
    state.micEnabled = false;
    setConn('ended');
    setAiStatus('offline');
    toggle(quality, false);
    syncMicUI();
    syncControls();
  }

  function leaveSession() {
    const wasActive = state.conn === 'connected' || state.conn === 'reconnecting';
    teardownSession();
    if (wasActive) {
      logSystem('已主动退出考场。', 'warn');
      showToast('已退出考场', 'info');
    }
  }

  function restartInterview() {
    closeReport(true);
    teardownSession();
    startInterview();
  }

  /* ------------------------------------------------------------------ *
   * Demo mode (fully local, no LiveKit, no microphone)
   * ------------------------------------------------------------------ */

  function clearDemoTimers() {
    while (demoTimers.length) {
      window.clearTimeout(demoTimers.pop());
    }
  }

  function estimateTypingDuration(text, options) {
    const opts = options || {};
    const chunk = opts.chunk || 3;
    const speed = opts.speed || 55;
    return 140 + Math.ceil(String(text).length / chunk) * speed + 500;
  }

  function demoType(role, text, options) {
    const opts = options || {};
    const speed = opts.speed || 55;
    const chunk = opts.chunk || 3;
    const key = 'demo:' + role + ':' + (++demoSegId);
    setTileSpeaking(role, true);
    if (role === 'examiner') setAiStatus('speaking');

    if (prefersReducedMotion()) {
      upsertSegment(role, key, text, true);
      setTileSpeaking(role, false);
      if (role === 'candidate') markCandidateFinished();
      else markExaminerFinished();
      if (typeof opts.onDone === 'function') opts.onDone();
      return estimateTypingDuration(text, opts);
    }

    let index = 0;
    function step() {
      index = Math.min(text.length, index + chunk);
      upsertSegment(role, key, text.slice(0, index), false);
      if (index < text.length) {
        demoTimers.push(window.setTimeout(step, speed));
      } else {
        upsertSegment(role, key, text, true);
        setTileSpeaking(role, false);
        if (role === 'candidate') markCandidateFinished();
        else markExaminerFinished();
        if (typeof opts.onDone === 'function') opts.onDone();
      }
    }
    demoTimers.push(window.setTimeout(step, 80));
    return estimateTypingDuration(text, opts);
  }

  function demoSeedConnectedScene() {
    logSystem(
      '演示模式（' + MODE_LABELS[interviewMode].name + '）：本地模拟面试进行中，不会连接 LiveKit，也不会采集麦克风。',
      'warn'
    );
    upsertSegment('examiner', 'demo:seed:1', DEMO_EXAM_1, true);
    upsertSegment('candidate', 'demo:seed:2', DEMO_CAND_1, true);
    upsertSegment('examiner', 'demo:seed:3', DEMO_EXAM_2, true);
    demoPartialKey = 'demo:seed:4';
    upsertSegment('candidate', demoPartialKey, DEMO_CAND_2_PARTIAL, false);
    setPhase('pro_qa', true);
    setAiStatus('thinking');
  }

  function initDemoConnectedScene() {
    demoSeedConnectedScene();
    const cycle = function () {
      if (!state.demoActive || state.conn !== 'connected') return;
      if (state.aiStatus === 'thinking') setAiStatus('listening');
      else if (state.aiStatus === 'listening') setAiStatus('thinking');
      demoTimers.push(window.setTimeout(cycle, 3600));
    };
    demoTimers.push(window.setTimeout(cycle, 3600));
  }

  function initDemoAutoScene() {
    // 完整本地流程：进入考场 → 阶段1 开场 → 考生实时转写 → AI 思考 →
    // 阶段2/3 多题追问（实战模式）→ 阶段4 成绩单弹窗 → 可手动退出。
    const script = DEMO_MODE_SCRIPT[interviewMode] || DEMO_MODE_SCRIPT.quick;
    logSystem(
      '演示模式（' + MODE_LABELS[interviewMode].name + '）：' +
      (interviewMode === 'real'
        ? '多题 + 多次追问，流程与真实面试一致（不会连接 LiveKit）。'
        : '题量小、节奏快，适合快速演示（不会连接 LiveKit）。'),
      'warn'
    );
    setPhase('self_intro');
    setAiStatus('waiting');

    function schedule(ms, fn) {
      demoTimers.push(window.setTimeout(fn, ms));
    }

    let currentPhase = 'self_intro';
    let index = 0;

    function finishInterview() {
      setPhase('report');
      setAiStatus('thinking');
      logSystem('本场面试结束，正在根据对话生成成绩单…', 'warn');
      schedule(1800, showDemoReport);
    }

    function runItem() {
      if (index >= script.length) {
        finishInterview();
        return;
      }
      const item = script[index++];
      if (item.phase && item.phase !== currentPhase) {
        currentPhase = item.phase;
        setPhase(item.phase);
        setAiStatus('thinking');
      }
      schedule(700, function () {
        const askDuration = demoType('examiner', item.examiner, {
          onDone: function () { setAiStatus('listening'); }
        });
        schedule(askDuration + 700, function () {
          const answerDuration = demoType('candidate', item.candidate, {
            onDone: function () { markCandidateFinished(); }
          });
          schedule(answerDuration + 900, function () {
            if (!item.followUp) {
              runItem();
              return;
            }
            const followDuration = demoType('examiner', item.followUp, {
              onDone: function () { setAiStatus('listening'); }
            });
            schedule(followDuration + 700, function () {
              const followAnswerDuration = demoType('candidate', item.followUpAnswer, {
                onDone: function () { markCandidateFinished(); }
              });
              schedule(followAnswerDuration + 900, runItem);
            });
          });
        });
      });
    }

    schedule(500, runItem);
  }

  function showDemoReport() {
    const data = normalizeReport(DEMO_REPORT);
    if (!data) return;
    state.report = { kind: 'structured', data: data };
    if (state.phase !== 'completed') setPhase('report');
    setAiStatus('offline');
    toggle(liveTag, false);
    openReport();
    logSystem('成绩单已生成，可在弹窗中查看。', 'success');
    syncControls();
  }

  function initDemoReportScene() {
    stopTimer();
    setConn('ended');
    toggle(liveTag, false);
    toggle(sessionTimer, true);
    setText(timerText, '08:42');
    logSystem(
      '演示模式（' + MODE_LABELS[interviewMode].name + '）：本场面试已完成，展示最终成绩单。',
      'warn'
    );
    const lines = [
      ['examiner', DEMO_EXAM_1],
      ['candidate', DEMO_CAND_1],
      ['examiner', DEMO_EXAM_2],
      ['candidate', DEMO_CAND_2_FULL],
      ['examiner', DEMO_COMPRE_Q],
      ['candidate', DEMO_COMPRE_A]
    ];
    lines.forEach(function (item, index) {
      upsertSegment(item[0], 'demo:report:' + index, item[1], true);
    });
    setPhase('report', true);
    setAiStatus('offline');
    state.micEnabled = false;
    const data = normalizeReport(DEMO_REPORT);
    state.report = { kind: 'structured', data: data };
    openReport();
    syncControls();
  }

  function initDemo() {
    state.demoActive = true;
    body.dataset.demo = '1';
    toggle(demoBadge, true);
    syncModeUI();
    setConn('connected');
    updateQuality('excellent');
    state.micEnabled = true;
    startTimer();

    if (DEMO_SCENE === 'report') initDemoReportScene();
    else if (DEMO_SCENE === 'connected') initDemoConnectedScene();
    else initDemoAutoScene();

    syncMicUI();
    syncControls();
    updateHints();
  }

  function restartDemo() {
    clearDemoTimers();
    if (state.thinkingTimer) {
      window.clearTimeout(state.thinkingTimer);
      state.thinkingTimer = null;
    }
    closeReport(true);
    if (transcriptEl) transcriptEl.replaceChildren();
    segments.clear();
    updateTranscriptCount();
    autoScroll = true;
    toggle(jumpLatest, false);
    stopTimer();
    toggle(sessionTimer, false);
    setText(timerText, '00:00');
    state.report = null;
    resetReportUI();
    setPhase(null, true);
    setAiStatus('idle');
    state.conn = 'idle';
    state.micEnabled = false;
    state.demoActive = false;
    toggle(quality, false);
    setConn('idle');
    initDemo();
  }

  function leaveDemo() {
    teardownSession();
    state.demoActive = false;
    setText(stageHintText, '演示已结束，可点击「再练一次」重新体验完整流程。');
    showToast('演示已结束', 'info');
    syncControls();
    syncMicUI();
  }

  /* ------------------------------------------------------------------ *
   * Page lifecycle
   * ------------------------------------------------------------------ */

  function handlePageHide() {
    if (state.pageHiding) return;
    state.pageHiding = true;
    clearDemoTimers();
    stopTimer();
    cleanupAudio();
    const room = state.room;
    state.room = null;
    if (room) {
      try { room.disconnect(); } catch (error) { /* noop */ }
    }
  }

  /* ------------------------------------------------------------------ *
   * Event binding
   * ------------------------------------------------------------------ */

  function bindEvents() {
    if (btnConnect) {
      btnConnect.addEventListener('click', function () {
        if (IS_DEMO) {
          if (state.conn === 'connected') {
            showToast('演示面试正在进行中。', 'info');
            return;
          }
          restartDemo();
          return;
        }
        startInterview();
      });
    }

    if (btnLeave) {
      btnLeave.addEventListener('click', function () {
        if (IS_DEMO) leaveDemo();
        else leaveSession();
      });
    }

    if (btnReport) {
      btnReport.addEventListener('click', openReport);
    }

    [[modeQuick, 'quick'], [modeReal, 'real']].forEach(function (pair) {
      if (pair[0]) {
        pair[0].addEventListener('click', function () { selectMode(pair[1]); });
      }
    });

    [micBtn, micToggle].forEach(function (button) {
      if (button) button.addEventListener('click', toggleMic);
    });

    if (playbackBtn) playbackBtn.addEventListener('click', restorePlayback);
    if (jumpLatest) {
      jumpLatest.addEventListener('click', function () {
        autoScroll = true;
        toggle(jumpLatest, false);
        if (transcriptScroll) transcriptScroll.scrollTop = transcriptScroll.scrollHeight;
      });
    }
    if (transcriptScroll) transcriptScroll.addEventListener('scroll', handleTranscriptScroll, { passive: true });

    if (errorRetry) {
      errorRetry.addEventListener('click', function () {
        const action = retryAction;
        hideError();
        if (action) action();
      });
    }

    if (reportClose) reportClose.addEventListener('click', function () { closeReport(); });
    if (reportCloseBtn) reportCloseBtn.addEventListener('click', function () { closeReport(); });
    if (reportAgain) {
      reportAgain.addEventListener('click', function () {
        if (IS_DEMO) restartDemo();
        else restartInterview();
      });
    }
    if (reportLayer) {
      reportLayer.addEventListener('keydown', handleReportKeydown);
      const backdrop = reportLayer.querySelector('[data-close-report]');
      if (backdrop) {
        backdrop.addEventListener('click', function () { closeReport(); });
      }
    }

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && reportLayer && !reportLayer.hidden) {
        closeReport();
      }
    });

    window.addEventListener('pagehide', handlePageHide);
    window.addEventListener('beforeunload', handlePageHide);
  }

  /* ------------------------------------------------------------------ *
   * Boot
   * ------------------------------------------------------------------ */

  function init() {
    if (!transcriptEl) return;
    if (reportScroll) reportScroll.setAttribute('tabindex', '0');
    bindEvents();
    syncModeUI();
    resetReportUI();
    syncControls();
    syncMicUI();
    setPhase(null, true);
    setAiStatus('idle');
    setConn('idle');

    if (IS_DEMO) {
      initDemo();
    } else {
      logSystem('系统提示：点击「进入考场」开始模拟面试，系统将自动申请麦克风权限。');
      logSystem('面试流程：自我介绍 → 专业问答 → 综合素质 → 综合成绩单。');
      window.setTimeout(function () {
        loadLiveKit().catch(function () { /* 连接时会再次尝试并给出内联错误 */ });
      }, 1500);
    }

    body.dataset.ready = '1';

    if (window.console && console.info) {
      console.info('[ai-interviewer] frontend ready', IS_DEMO ? '(demo mode: ' + DEMO_SCENE + ')' : '');
    }

    if (params.get('debug') === '1') {
      // Development-only hooks: lets the data-channel contract be exercised
      // without a live room (?debug=1). Never installed in normal use.
      window.__interviewDebug = {
        data: handleDataMessage,
        transcription: handleTranscription,
        setLocalIdentity: function (identity) { state.localIdentity = String(identity || ''); },
        setPhase: setPhase,
        setStatus: setAiStatus,
        setConn: setConn,
        openReport: openReport,
        closeReport: closeReport,
        toast: showToast
      };
      runLayoutDebug();
    }
  }

  function runLayoutDebug() {
    (function () {
      const viewport = window.innerWidth;
      const lines = [];
      lines.push('vw=' + viewport + ' docSW=' + document.documentElement.scrollWidth + ' bodySW=' + document.body.scrollWidth);
      ['.app-shell', '.topbar', '.topbar-meta', '.workbench', '.stage-panel', '.transcript-panel', '.controlbar', '.controlbar-inner', '.stepper', '.tiles'].forEach(function (selector) {
        const node = document.querySelector(selector);
        if (!node) return;
        const rect = node.getBoundingClientRect();
        lines.push(selector.replace(/^\./, '') + ' L' + Math.round(rect.left) + ' W' + Math.round(rect.width) + ' SW' + node.scrollWidth);
      });
      const offenders = [];
      Array.prototype.forEach.call(document.querySelectorAll('body *'), function (node) {
        if (node.closest && node.closest('.ambient')) return;
        if (!node.getBoundingClientRect) return;
        const rect = node.getBoundingClientRect();
        if (rect.width === 0) return;
        if (rect.right > viewport + 0.5) {
          let name = node.tagName.toLowerCase();
          if (node.id) name += '#' + node.id;
          else if (typeof node.className === 'string' && node.className.trim()) name += '.' + node.className.trim().split(/\s+/)[0];
          offenders.push(name + ' R' + Math.round(rect.right) + ' W' + Math.round(rect.width));
        }
      });
      offenders.sort(function (a, b) { return b.length - a.length; });
      lines.push('--- offenders ' + offenders.length);
      offenders.slice(0, 10).forEach(function (item) { lines.push(item); });
      const pre = document.createElement('pre');
      pre.id = 'layout-debug';
      pre.style.cssText = 'position:fixed;left:0;top:0;z-index:9999;width:390px;max-height:520px;overflow:hidden;margin:0;padding:6px;background:#fff;color:#000;font:11px/1.35 Consolas,monospace;white-space:pre;';
      pre.textContent = lines.join('\n');
      document.body.appendChild(pre);
    })();
  }

  init();
})();
