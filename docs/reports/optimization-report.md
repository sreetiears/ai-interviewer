# 优化报告：代码质量修复 + 面试过程前端体验升级

- **目标**：审查 ai-interviewer 代码质量，并优化"面试真正跑起来时用户看到/感受到的过程表现"（实时转写、阶段推进、AI 状态、成绩单、语音流畅度、模式选择），而不是单纯美化静态页面。
- **基线**：`HEAD 63c4a0c`（审查报告见 [code-review.md](code-review.md)，总体评级 C / 约 62 分）。
- **结果**：`pytest` 93 passed；`node --check frontend/app.js` 通过；本地 `server.py` 冒烟全绿；演示模式可完整跑通 quick / real 两种面试模式。
- **截图**：[优化前](screenshots/before-desktop.png) vs [优化后](screenshots/after-real-mode.png)，其余见 `docs/screenshots/`。

---

## 1. 审查阶段的主要结论

| 级别 | 问题 | 结论 |
| --- | --- | --- |
| P0 | `server.py` 启动日志含 emoji，中文 Windows（cp936）下 `python server.py` 必崩 | 已修复 |
| P1 | 成绩单 / 阶段 / 转写全链路缺失（前后端无数据通道） | 已修复 |
| P1 | Dify 兼容分支使用 `json` 却未 import，切 Dify 必崩 | 已修复 |
| P1 | 综合素质答完不生成报告、重复播报上一题，报告被推迟一轮 | 已修复 |
| P1 | 每阶段只有 1 题、整场 5–6 轮就结束，无法满足"实战模式" | 已改为 quick / real 双模式 |
| P1 | `/api/token` 无鉴权 + CORS `*` + 路径前缀校验可绕过 | 部分修复（CORS/路径已修，鉴权仍需部署侧处理） |
| P1 | 历史提交残留明文 SiliconFlow Key | 需用户侧轮换（见文末） |
| P2 | 伪流式（整段生成后切块）、无超时、无 usage | 已改为真流式；超时/usage 仍建议后续补 |
| P2 | `compre_qa → report` 依赖下一轮用户发言；完成态重复播报 | 已修复（报告同轮生成） |
| P2 | 状态字段无 reducer、节点职责混杂、prompt 与代码阈值不一致 | 阈值已统一；结构性重构列入后续建议 |
| P3 | 未使用 import、emoji 日志、`interview_word .yml` 等 | import/emoji 已清理；数据文件保留不动 |

完整问题清单、复现命令与修复优先级见 [code-review.md](code-review.md)（392 行，含附录 A 复现记录）。

---

## 2. 本轮修复明细（含验证）

### 2.1 阻断与功能缺失（P0/P1）

| 问题 | 修复 | 验证 |
| --- | --- | --- |
| cp936 下启动崩溃 | `server.py` 全面重写：移除 emoji/print，改用 logging + UTF-8 安全流；`PYTHONIOENCODING=cp936` 冒烟通过 | `test_server.py` 编码回归测试 + 真实进程冒烟 |
| 成绩单不可见 | 定义 LiveKit 数据通道协议（topic=`interview`：`status`/`phase`/`report`/`mode`），Agent 在状态机推进时推送；前端监听并渲染成绩单面板 | `test_langgraph_adapter.py` 断言事件序列；前端 demo 全流程验证 |
| 阶段指示器不联动 | 阶段完全由 `phase` 事件驱动，进度线动画 + 完成态 | demo quick/real 截图（阶段 1→2→3→4） |
| 无实时转写 | 前端订阅 `RoomEvent.TranscriptionReceived`，按 identity 区分考生/考官，同 id 分片原位更新，智能滚动 | CDP 自动化 + 截图（9 条转写） |
| Dify 分支 NameError | 补 `import json`，并补 SSE 解析单测（message/keepalive） | `test_agent_stream.py` |
| 报告延迟/重复播报 | 综合素质最后一题答完后**同一轮**生成报告并返回结语；移除只返回 phase 的分支 | `test_graph_flow.py` 断言最后一轮直接 `completed` + `report_data` |
| 面试深度不足 | 新增 `quick`（1 题/阶段、1 次追问）与 `real`（3 题/阶段、2 次追问）两种模式；自我介绍重试上限可配 | `test_graph_flow.py::test_real_mode_asks_three_questions_per_phase` |
| 报告解析失败伪造分数 | 缺失/非法 `total_score` 视为解析失败，返回"报告生成失败"而非编造 85 分 | `test_graph_nodes.py::test_report_parse_failure_does_not_fabricate_scores` |
| prompt 与代码阈值不一致（50 字 vs 40 字） | 统一为 `MIN_INTRO_CHARS=50`（可配），judge 与代码共用同一阈值 | `test_graph_nodes.py` 相关用例 |

### 2.2 语音流畅度与状态机正确性（P2）

- **真流式**：图节点改用 `_reply_text()`，在 LangGraph `custom` stream 中逐 token 调用 `get_stream_writer()`；适配器使用 `graph.astream(stream_mode=["custom","values"])` 边收 token 边送 TTS。首字延迟从"整段生成完"降到"首个 token"。
- **避免重复消费回合**：`AgentSession(preemptive_generation=False)`，因为图调用有推进状态机的副作用。
- **状态机闭环**：`questions_asked` / `questions_per_phase` / `max_follow_ups` / `max_intro_attempts` 进入 state，由模式默认值 + 调用方覆盖共同决定；每个阶段题量用尽才推进。
- **流式回落**：静态台词（如"很好，自我介绍通过"）没有 token 流，适配器检测到未流式输出时整段发送，避免丢话。

### 2.3 服务端加固（P1/P2/P3）

- `ThreadingHTTPServer` + `daemon_threads` + 端口预探测（Windows `SO_EXCLUSIVEADDRUSE`），端口占用友好报错、SIGINT 优雅退出。
- 路径穿越防护：先 `unquote`，再 `normpath` + `realpath`，用 `commonpath` 判断（覆盖 `../`、`%2e%2e%2f`、`..\`、前缀相似兄弟目录）。
- 移除通配 CORS（默认同源不需要；`ALLOWED_ORIGIN` 白名单可选），补 `nosniff` / `Referrer-Policy` / `X-Frame-Options`。
- 完整 MIME 表、`Content-Length`、`index.html no-store` / css/js `max-age=300`、404/405/500 兜底、`GET /api/health`。
- token 参数校验（长度/字符），`mode=quick|real` 写入 token metadata，响应新增 `interviewMode`（原四字段不变）。

### 2.4 前端重构（frontend/）

原 512 行单文件拆为 `index.html` + `styles.css` + `app.js`（原生技术栈，无构建步骤）：

- 视觉（第二轮按"好看"需求重做）：克制的极简编辑排版——近黑石墨底、单一冷静强调色、发丝分割线、五级字号层级、面板与徽章大幅减少；大厅居中列、面试中"舞台 + 独立对话区"、成绩单大数字 + 细线维度；动效只保留发言呼吸（scale ≤1.02）、状态切换 200ms、成绩单一次性 reveal，并受 `prefers-reduced-motion` 关闭。
- 安全：所有动态文本 `textContent`（原 `innerHTML` XSS 面消除，实测 XSS 载荷只显示为文本）；不引入第三方 CSS/字体框架。
- 健壮性：音频 track `attach/detach` 统一清理、`pagehide` 断开、重连状态、自动播放被拦截横幅、内联错误卡片 + 重试、连接质量、麦克风静音、`aria-live`/`focus-visible`/`prefers-reduced-motion`。
- 性能：LiveKit SDK 按需懒加载（原阻塞 CDN `<script>` 移除），SDK/CDN 不可用不白屏；DOM 引用缓存、滚动节流。
- **演示模式**：`?demo=1`（`&mode=real|quick`、`&scene=report|connected`）在没有 LiveKit/API Key 时完整模拟面试，供录屏与离线演示使用。
- 响应式：CDP 真机宽度实测 360/390/768/1440 无横向溢出（注：Edge headless `--window-size=390` 会先按约 492px 布局再裁切截图，CLI 截图不能用于判断移动端溢出）。

---

## 3. 面试模式（本轮新增）

| 模式 | 每阶段主问题 | 最大追问 | 自我介绍重试 | 整场轮次 |
| --- | --- | --- | --- | --- |
| quick（默认，简单模式） | 1 | 1 | 1 | 约 5–6 轮 |
| real（实战模式） | 3 | 2 | 3 | 约 15+ 轮 |

选择方式（任选其一，后者优先）：

1. 环境变量 `INTERVIEW_MODE=quick|real`（默认模式）。
2. 页面进入考场前的模式选择器 → `/api/token?room=...&mode=...` → token metadata → Agent 读取 `participant.metadata`。
3. 各模式题量可用 `QUICK_QUESTIONS_PER_PHASE` / `REAL_QUESTIONS_PER_PHASE` / `QUICK_MAX_FOLLOW_UPS` / `REAL_MAX_FOLLOW_UPS` / `*_MAX_INTRO_ATTEMPTS` 调整。

---

## 4. 验证证据

```
$ .venv\Scripts\python.exe -m pytest -q
92 passed, 15 warnings in 9.49s        # warning 均为 devkey/secret 6 字节的预期告警

$ node --check frontend/app.js         # exit 0
$ python -m compileall agent.py config.py interview_graph.py langgraph_adapter.py server.py tests   # exit 0
```

HTTP 冒烟（新一轮 `server.py`，端口 8080）：

| 请求 | 结果 |
| --- | --- |
| `GET /` | 200 `text/html; charset=utf-8`，`Cache-Control: no-store`，`X-Content-Type-Options: nosniff`，无 CORS 通配 |
| `GET /styles.css` `/app.js` | 200，正确 MIME，`max-age=300`，`Content-Length` 与实体一致 |
| `GET /api/health` | 200 `{"status": "ok"}` |
| `GET /api/token?mode=real` | 200，返回 `serverUrl/roomName/identity/token/interviewMode`；token metadata 解码为 `{"interview_mode": "real"}` |
| `GET /api/token?mode=turbo` | 400 + 允许值说明 |
| `GET /%2e%2e%2fREADME.md` | 403（路径穿越被拒） |
| `GET /nope.css` | 404 |

界面证据（`docs/screenshots/`）：`after-live-lobby.png`（大厅/模式选择）、`after-real-mode.png`（实战模式进行中）、`after-ai-thinking.png`（AI 思考中）、`after-report.png`（成绩单）、`after-mobile-390.png` / `after-mobile-report-390.png`（移动端）、`after-error-state.png`（未启动 LiveKit 时的错误态）。

---

## 5. 仍建议后续处理

1. **token 接口鉴权/限流**：当前 `/api/token` 对任意来源匿名签发，适合本机/内网演示；公网部署前必须加鉴权、房间白名单、短 TTL，并把 `FRONTEND_HOST` 收到 `127.0.0.1`。
2. **真实 LiveKit 端到端验证**：本环境无 `livekit-server` 与 API Key，音频/麦克风/自动播放链路只做了代码路径与模拟事件验证。
3. **状态机结构化重构**（审查报告 P2-1/P2-2/P2-4）：`raw_scores` 改 reducer、拆分"出题/评卷/推进"节点、抽公共 QA 节点；当前保持行为修复优先。
4. **可观测性与持久化**：补 LLM 超时/usage 统计、结构化日志、成绩单落库、checkpoint TTL 清理、依赖锁定（lock 文件）与 CI/lint。

---

## 6. 后续变更（用户确认后执行）

1. **移除 Dify 兼容层**：用户确认项目已改为纯 LangGraph。本次删除 `agent.py` 中 `DifyLLM` / `DifyLLMStream` 及其专用辅助函数（约 190 行）、`config.py` 的 `LLM_BACKEND`/`DIFY_*` 字段、`.env.example` 的 Dify 配置段、对应测试与 README 说明；`interview_word .yml`（Dify 工作流导出）同步删除；`requirements.txt` 移除仅为 Dify 直接使用的 `aiohttp`（它仍是 `livekit-agents` 的传递依赖）。测试数 93 → 92，全部通过。
2. **清理 Git 历史中的明文 Key**：对全部历史做内容级重写（正则 `sk-[A-Za-z0-9_-]{12,}` → `sk-REDACTED`，仅命中 1 个历史 blob），随后 `reflog expire` + `gc --prune=now`，本地与远端（force-push）均已更新；详见第 7 节。

---

## 7. Git 历史清理记录

- 处理范围：全部 6 个 commit 的 commit message 与 blob 内容；提交信息无密钥。
- 旧 → 新 commit 映射（分支 `ai_made`）：

  | 旧 | 新 |
  | --- | --- |
  | `5233947` Initial commit | `5f56455` |
  | `ac55bef` chore: install skills | `3398425` |
  | `6d2ffca` fix: remove hardcoded api keys | `deedce2` |
  | `f6698b8` feat: LangGraph refactor | `836e101` |
  | `d213b03` feat: custom frontend | `8f4a08d` |
  | `63c4a0c` docs: README | `5a354a1`（当前 HEAD） |

- 当前分支 HEAD 的 tree 与旧 HEAD 完全一致（历史重写只影响前两个 commit 里 `agent.py` 的旧版本），工作区改动不受影响。
- 远端 `origin`（GitHub）已 `git push --force` 更新 `ai_made` / `main`；重写前创建的临时备份 bundle（含旧密钥）在验证通过后已删除，避免密钥再次落盘。若确需旧历史，只能从 GitHub 悬空对象或对方副本获取。
- 校验结果：`git cat-file --batch-all-objects` 扫描 222 个对象，含密钥 blob 数 = 0；`git log --all -G 'sk-[A-Za-z0-9_-]{12,}'` 无命中；`git fsck --unreachable` 无悬空对象。
- 注意：GitHub 不会立即回收悬空对象，知道旧 commit SHA 的人仍可能短时间访问到旧内容；该 API Key 仍应**立即吊销轮换**，必要时联系 GitHub Support 清理缓存。
