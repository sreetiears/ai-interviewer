# AI Interviewer 答辩讲稿（长期记忆修订版）

全稿排练目标约5分20秒—5分50秒，中心预算5分30秒；仅为讲述安排，未录音计时，不保证实际时长。参考与边界标为“不朗读”，不计入正文。若排练接近6分钟，应进一步压缩串场，不删去实现边界。

## 第1页｜预算约20秒

各位评委好。我们带来的作品是 AI Interviewer，一款面向考研复试的 AI 语音面试官。它以实时语音主持模拟面试，帮助学生走完完整流程，并在结束后获得反馈。

【参考｜不朗读】作品定位见已确认大纲。实时语音、阶段流程与结束即时反馈见仓库 README：https://github.com/sreetiears/ai-interviewer/blob/ai_made/README.md
竞赛强调用智能体解决教育等实际场景：https://www.ai-race.com.cn/
右侧为能力示意，不是产品截图。

讲述预算约20秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第2页｜预算约25秒

考研复试前，学生常会碰到三个具体困难：缺少可以反复练习的面试伙伴，缺少针对当场回答的追问，以及练完以后缺少有效复盘。我们不把它写成调查统计，而是作为产品要回应的真实需求。

【参考｜不朗读】痛点三分法见已确认大纲，未引用调研数据或比例。竞赛导向：https://www.ai-race.com.cn/

讲述预算约25秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第3页｜预算约35秒

面试不是一次跑完所有环节，而是按回答逐轮推进。自我介绍不足时先补问，合格或补问次数用尽后进入专业问答，再进入综合考察。这两个阶段共用一套判断循环：提问、作答、判断。需要深入且未达上限时追问，否则继续下一道主问题；本阶段结束就前进。综合考察完成后，在当轮直接生成报告，不需要考生再说一句话。

【参考｜不朗读】业务状态与判断条件见 interview_graph.py（ai_made 分支）：
https://github.com/sreetiears/ai-interviewer/blob/ai_made/interview_graph.py
create_interview_graph 第711–740行：set_conditional_entry_point(route_phase) 进入 self_intro / pro_qa / compre_qa / report / completed（end 映射到 completed）；各节点 add_edge(..., END)，并以 MemorySaver 检查点保存状态。因此本图箭头是跨轮次业务状态，不是编译后的图边。
自我介绍 node_self_intro 第273–317行：不合格且仍有补问次数则补充并留在 self_intro；不合格但次数用尽，或介绍合格，则进入 pro_qa。
专业问答 node_pro_qa 第338–434行：无待答则提问；作答后判断，需追问且未达上限则追问；否则若本阶段题量未满则下一主问题；否则转入 compre_qa。
综合考察 node_compre_qa 第454–538行：同一套分支；题量完成后在当轮调用 _generate_report，不额外等待用户发言。
node_completed 第679–689行：面试结束后若考生再发言，只给结束提醒（本页未单独画成节点）。
route_phase 第695–705行：按 current_phase 选择入口节点。

讲述预算约35秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第4页｜预算约30秒

这一页是仓库已有的演示模式界面，不是本次新录的实测。右侧按轮次显示考官和考生的对话转写，左上角显示面试阶段，状态区提示考官正在提问。考生既能开口作答，也能回看对话、知道面试进度。截图只说明界面体验，不代表真实音频全链路验证。

【参考｜不朗读】截图目录：https://github.com/sreetiears/ai-interviewer/tree/ai_made/docs/screenshots
文件：https://github.com/sreetiears/ai-interviewer/blob/ai_made/docs/screenshots/after-real-mode.png
界面中的增量转写、阶段进度、思考/播报/收听状态，见 README：https://github.com/sreetiears/ai-interviewer/blob/ai_made/README.md

讲述预算约30秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第5页｜预算约30秒

智能体主持面试，靠四类决策，而不是固定念稿：按当前阶段进入环节；听完回答判断是否深入；需要且未达上限时追问；本阶段题量结束就前进，综合考察结束则当轮出报告。前面展示的是这些决策跨轮次如何衔接，这里强调判断本身。

【参考｜不朗读】interview_graph.py（ai_made）：
https://github.com/sreetiears/ai-interviewer/blob/ai_made/interview_graph.py
node_self_intro 第273–317行：介绍不足且仍有补问次数则补充提问。
node_pro_qa 第338–434行：判断后按需追问，题量未满则下一主问题，否则转入综合考察。
node_compre_qa 第454–538行：同一套判断，题量结束则当轮生成报告。
create_interview_graph 第711–740行：按当前阶段进入节点，每轮结束后保存检查点。

讲述预算约30秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第6页｜预算约33秒

语音交互的关键，是让生成和播出衔接起来。考生的声音先被转写，模型生成回复时按 token 向外发送文本增量，增量随即进入语音输出链路，让语音合成可以开始播报，而不必等整段文字写完。阶段过渡等没有流式增量的轮次，则发送完整回复。这一页讲的是实现机制，我们还没有可引用的端到端延迟实测，不把它说成无延迟，也不承诺具体提速。

【参考｜不朗读】langgraph_adapter.py：graph.astream(..., stream_mode=["custom", "values"])，custom 模式下 type=delta 的逐 token 增量通过 _send_chunk 送出，注释写明语音合成可以从第一个 token 开始；若本轮没有流式增量，则把整段回复一次送出。
https://github.com/sreetiears/ai-interviewer/blob/ai_made/langgraph_adapter.py
README：LiveKit 实时音频、SiliconFlow STT/TTS、自定义 token 流。https://github.com/sreetiears/ai-interviewer/blob/ai_made/README.md
agent.py：AgentSession 接入 SiliconFlow 的转写与语音合成。

讲述预算约33秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第7页｜预算约35秒

实时部分，LiveKit 负责收音、转写和播出，LangGraph 负责阶段路由和追问判断。本场状态由进程内的 MemorySaver 保存；界面接收决策侧的阶段、状态和报告事件，转写则直接来自语音会话。跨场档案另存 SQLite：结束后写入成绩单、掌握度与已问历史，下一场入场时再读取摘要参与出题。它不在实时语音链路里，也不替代本场检查点。

【核验与参考｜不朗读】仅依据固定提交做静态源码核验，未运行应用、测试或真实 WPS。MemorySaver 仍是进程内检查点；SQLite 是独立跨场存储，写入失败会记录日志，不阻断成绩单，缺少 learner_id/session_id 会跳过。README L239“未落库”是旧文档，与 record_session 写入 report_json 的代码冲突，以代码为准。
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/interview_graph.py#L962-L963
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/interview_graph.py#L804-L895
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L76-L124
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L497-L558
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/agent.py#L335-L377
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/README.md#L239
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/README.md#L246
原有媒体/界面关系沿用已交付架构；决策与适配器处理事件，不是检查点直接发事件。

讲述预算约35秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第8页｜预算约42秒

本场记忆负责接着问：阶段、题目和评分留在状态里，同一阶段避免重复，换阶段清空已问列表；每次面试也用独立的会话标识。跨场则形成另一个闭环：结束时把成绩单、知识点掌握度和已问历史写入 SQLite；下一次入场，把薄弱点和跨场已问摘要带进出题提示，让面试从薄弱点继续，也支持按建议方向加强再练。这里用同一浏览器保存的 learner_id 关联历次记录，session_id 仍然每场独立。

【核验与参考｜不朗读】静态源码核验，未运行应用或测试。身份是 localStorage 的本地标识，不是登录账号，不支持已核验的跨设备同步。Agent 入场时加载一次档案快照，不是逐轮向量搜索；当前没有 embeddings/RAG，也没有已实现的习惯偏好画像。知识点由 judge LLM 在封闭 topic 集中选择，掌握度按 verdict 规则更新；显式 focus 优先，否则选择有效掌握度低于0.5的方向（最多3个，另有无薄弱项回退）。写入失败不阻断报告，加载失败可继续面试；不承诺每次存储成功或已测得提升。
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/interview_graph.py#L200-L213
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/interview_graph.py#L278-L294
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/interview_graph.py#L804-L895
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L24-L69
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L213-L231
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L334-L416
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L497-L558
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/frontend/app.js#L318-L330
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/frontend/app.js#L2083-L2141
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/server.py#L179-L214
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/agent.py#L54-L100
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/agent.py#L335-L377
本阶段 asked_questions 的阶段清空行为沿用原交付说明；跨场 asked_history 是另一份持久化历史。README L239“未落库”已过时，见 record_session 实现。

讲述预算约42秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第9页｜预算约32秒

面试结束后，系统当场生成成绩单，包含总分、三个维度评分、核心不足和改进建议。报告与本场表现会写入 SQLite 档案，给后续练习提供参考。这里右边是演示成绩单，左边放大了同一张图的建议部分，方便看清内容；这些分数和评语是示例，不代表真实考生结果或已经测得的辅导效果。

【实现与来源｜不朗读】静态源码核验，未运行应用或测试。_generate_report 构建报告后调用 _persist_learner_memory，再调用 LearnerStore.record_session；sessions.report_json 保存报告 JSON，同时更新 topic_mastery 和 asked_history。缺少 learner_id/session_id 会跳过写入；写入异常只记录警告，不阻断成绩单显示；报告 JSON 解析回退时仍会尝试保存知识点标签及精简结果。持久化不等于保证每次写入成功。源码是当前归档行为的依据，旧文档冲突见 evidence.md。
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/interview_graph.py#L804-L895
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L76-L124
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L497-L558
【截图溯源｜不朗读】保留原交付同一 after-report.png 的成绩单卡片与建议局部，两张图未替换、未改字、未重绘。
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/docs/screenshots/after-report.png
原交付卡片裁切 (408,58,1030,836)，建议局部裁切 (424,502,1016,836)；去掉卡片底部按钮。屏幕明确标注“演示模式报告（示例）”，不是本次新功能的运行验证截图。

讲述预算约32秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。

## 第10页｜预算约48秒

应用价值可以概括为三点：用语音完整走过复试流程；结束后用评分、核心不足和建议做复盘；保存知识点表现，让下一场结合薄弱点和已问历史继续练习。未来，我们会把历次记录整理成持续更新的能力画像，并建设 RAG，也就是检索增强生成知识库，把学习资料带入问题设计和反馈，再结合画像与薄弱项调整练习重点。我们也会收集真实反馈，持续打磨体验。右侧是未来方向，与当前已实现的 SQLite 档案区分开。

【计划与实现边界｜不朗读】个人画像与 RAG 知识库是用户明确授权的未来计划，没有声称已实现或已有测得的提升。当前实现是同一浏览器 localStorage learner_id 关联的结构化 SQLite 档案，包括报告、掌握度和已问历史；Agent 入场时读取快照注入出题提示，不是逐轮向量检索。kb_facts 仅有预留表与读写辅助函数，没有外部调用以实现习惯/偏好画像，也不应称为当前 RAG。无登录账号或跨设备同步主张。本次只做静态源码核验，未运行应用、测试或真实 WPS。
【当前实现参考｜不朗读】
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L1-L5
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L76-L124
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L213-L231
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/learner_store.py#L334-L416
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/frontend/app.js#L318-L330
https://github.com/sreetiears/ai-interviewer/blob/106a0ee70398aadf608aaea7d49fcbf9306c4432/agent.py#L335-L377
未来 RAG/个人画像：用户指定展望，不以当前源码作为已实现证据。

讲述预算约48秒；全稿排练目标5分20秒—5分50秒，未录音计时，不保证实际时长。
