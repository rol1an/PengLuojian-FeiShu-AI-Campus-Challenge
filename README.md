<div align="center">

[中文](#飞书会议助手) · [English](#feishu-meeting-assistant)

</div>

---

# 飞书会议助手

> 基于豆包大模型的飞书会议自动化助手，会前融合聊天记录与知识库推送「会前同步卡」，会后自动生成 Action Items 并创建飞书任务。

<div align="center">

**飞书 AI 校园挑战赛 · OpenClaw 赛道参赛项目**

</div>

## 功能演示

### 会前：「会前同步卡」推送

会议开始前 25 分钟，系统自动检测飞书日历，**并发融合四路信息**，经 LLM 两步提炼后生成「会前同步卡」推送给所有参会人。

**四路信息融合**：
1. 会议基础信息（标题 / 议程 / 参会人 / 发起人）
2. 当前用户与所有参会人的私聊 DM（近 14 天，最新 10 条）
3. 会议邀请群聊的上下文（从邀请描述中提取 openChatId，近 7 天）
4. 知识库文档（质量过滤 → 语义精排 → 文档富化 → LLM 去重）

**卡片三段式结构**：
- **最近上下文**：LLM 两步提炼——先从原始消息中选 3-5 句最相关原句，再改写成 40-50 字自然句（含主体 + 进展），每条附 📅 溯源标签（时间 + 来源对话/群聊）
- **关键材料**：top 3 相关文档，含 📄 相关说明 + 📌 关键结论
- **待确认问题**：LLM 从上下文中提炼 1-3 条需在会中解决的问题

![会前同步卡](picture_data/new_premeetexample1.png)

### 会中：Q&A 会议助手追问

收到「会前同步卡」后，可在机器人私聊中直接追问会议相关问题。助手会实时从知识库检索相关文档，结合近期私聊/群聊记录给出回答，并内联注明来源（文档附链接，私聊说联系人）。

**核心机制**：
- **章节级精准检索**：对文档按标题切分章节，用问题关键词打分，只取最相关的章节（≤ 3000 字）传入 LLM，避免长文档被位置截断截掉关键内容
- **来源可追溯**：文档来源自动附链接，私聊来源注明联系人姓名，群聊来源注明群名
- **自然汇报风格**：回答遵循「先结论→再来源→最后收口」的隐含顺序，而非固定结构化模板

![Q&A 会议助手](picture_data/Q&AAgent.png)

### 服务运行日志

服务每 60 秒轮询日历，自动触发关键词提取 → Wiki 搜索 → LLM 重排 → 文档富化 → 卡片推送全流程。

![服务运行日志](picture_data/example3.png)

### 会后：自动生成任务并分配责任人

会议结束后，系统监听 `vc.meeting.end` 事件，自动拉取会议转写，提取 Action Items 并创建飞书任务，自动识别责任人、设置截止时间，并关联相关知识文档。

![会后任务列表](picture_data/postmeetexample1.png)

### 会后：任务详情（责任人 + 截止时间 + 知识文档）

每条任务包含：来自会议的背景说明、责任人、截止日期，以及自动关联的相关知识库文档链接。

![会后任务详情](picture_data/postmeetexample2.png)

## 技术架构

```
工作流 1 · 会前推送
飞书日历（APScheduler 轮询，会议前 25 分钟触发）
  → 并发四路信息融合
      ├─ 私聊 DM（与所有参会人，近 14 天）
      ├─ 会议群聊上下文（近 7 天）
      └─ 知识库文档
            → LLM 关键词提取
            → docs +search 全文搜索
            → 质量过滤（时效 / 类型 / 标题 / 发起人加成）
            → LLM 语义精排（0-10 分）
            → LLM 去重（跨知识库相同内容只留最新版）
            → 文档正文富化（关键结论 / 待确认问题）
  → LLM 两步上下文提炼（SELECT 关键句 → SYNTHESIZE 自然句 + 溯源标签）
  → 会前同步卡推送给参会人

工作流 2 · 会中 Q&A
用户在机器人私聊发送问题（im.message 事件监听）
  → 从 context_store 读取会前简报（TTL 4 小时）
  → 实时 wiki 搜索（用问题作为关键词）
       → 章节级精准提取（按标题切分 → 关键词打分 → top 章节 ≤ 3000 字）
  → 融合会前简报 + 实时检索内容
  → LLM 生成自然语言回答（先结论→来源内联→收口）

工作流 3 · 会后处理
飞书 vc.meeting.end 事件（WebSocket 监听）
  → 拉取会议转写 / 妙记
  → LLM 提取 Action Items（结构化 JSON）
  → Wiki 关联（每条 Action Item 匹配相关文档）
  → 自动创建飞书任务并分配给执行人
```

## 技术栈

| 组件 | 技术 |
|------|------|
| LLM | 豆包 2.0（Volcengine Ark，OpenAI 兼容接口）|
| 飞书能力 | lark-cli（日历 / IM / Wiki / 任务 / 视频会议）|
| 服务框架 | FastAPI + APScheduler |
| 运行环境 | Python 3.11+，uvicorn |

## 快速开始

**1. 安装依赖**
```bash
pip install -e .
```

**2. 配置环境变量**
```bash
cp .env.example .env
# 编辑 .env，填入以下必填项：
# DOUBAO_API_KEY=your_api_key
# LLM_MODEL=your_endpoint_id
# WIKI_SPACE_ID=your_wiki_space_id
```

**3. 启动服务**
```bash
uvicorn app.main:app --port 8080
```

**4. 调试端点**
```bash
# 手动触发会前推送（测试 LLM 关键词提取 + Wiki 搜索 + 卡片推送）
curl -X POST http://localhost:8080/debug/trigger-premeet \
  -H "Content-Type: application/json" \
  -d '{"event_id":"test-001","title":"会议标题","description":"议程描述","attendee_open_ids":["ou_xxx"]}'

# 手动触发会后流程（dry_run=true 仅查看结果，不实际建任务）
curl -X POST http://localhost:8080/debug/trigger-postmeet \
  -H "Content-Type: application/json" \
  -d '{"meeting_id":"your_meeting_id","dry_run":true}'
```

---

# Feishu Meeting Assistant

> An automated Feishu meeting assistant powered by Doubao LLM. Fuses chat context and knowledge base to push a pre-meeting briefing card, then auto-generates Action Items and Feishu tasks after meetings.

<div align="center">

**Feishu AI Campus Challenge · OpenClaw Track**

</div>

## Demo

### Pre-meeting: "Meeting Briefing Card" Push

25 minutes before a meeting, the system detects the calendar event and **concurrently fuses four information sources**, then uses a two-step LLM pipeline to generate and push a structured briefing card to all attendees.

**Four information sources (concurrent)**:
1. Meeting basics (title / agenda / attendees / organizer)
2. Direct message history with all attendees (last 14 days, top 10 messages)
3. Group chat context from the meeting invite (last 7 days, if a group link is embedded)
4. Knowledge base documents (quality filter → semantic reranking → enrichment → deduplication)

**Three-section card structure**:
- **Recent Context**: Two-step LLM synthesis — select 3-5 most relevant sentences from raw messages, then rewrite each into a 40-50 char natural sentence (subject + content + status), with a 📅 source label (timestamp + DM partner / group name)
- **Key Materials**: Top 3 relevant docs with 📄 relevance note + 📌 key conclusion
- **Open Questions**: 1-3 unresolved points LLM extracted from context

![Pre-meeting Briefing Card](picture_data/new_premeetexample1.png)

### In-meeting: Q&A Follow-up Assistant

After receiving the briefing card, attendees can ask follow-up questions directly in the bot's DM chat. The assistant performs real-time wiki search, combines knowledge base results with recent chat history, and replies with inline source attribution.

**How it works**:
- **Section-level retrieval**: Splits documents by heading, scores each section against the question's keywords, and passes only the most relevant sections (≤ 3,000 chars) to the LLM — ensuring long documents don't get truncated before the relevant part
- **Traceable sources**: Document sources include clickable links; DM sources name the contact; group chat sources name the group
- **Natural reporting style**: Answers follow an implicit order — conclusion first, then source attribution, then a closing remark — rather than a rigid templated format

![Q&A Meeting Assistant](picture_data/Q&AAgent.png)

### Service Logs

The service polls the calendar every 60 seconds and triggers the full pipeline: keyword extraction → Wiki search → LLM reranking → document enrichment → card push.

![Service Logs](picture_data/example3.png)

### Post-meeting: Auto-create Tasks with Assigned Owners

After the meeting ends, the system listens for the `vc.meeting.end` event, fetches the transcript, extracts Action Items, and creates Feishu tasks — automatically identifying owners, setting due dates, and linking relevant wiki documents.

![Post-meeting Task List](picture_data/postmeetexample1.png)

### Post-meeting: Task Detail (Owner + Due Date + Wiki Links)

Each task includes meeting context, assignee, due date, and automatically linked knowledge base documents.

![Post-meeting Task Detail](picture_data/postmeetexample2.png)

## Architecture

```
Workflow 1 · Pre-meeting Push
Feishu Calendar (APScheduler polling, triggers 25 min before meeting)
  → Concurrent 4-source fusion
      ├─ DM history with all attendees (last 14 days)
      ├─ Meeting group chat context (last 7 days)
      └─ Knowledge base docs
            → LLM keyword extraction
            → docs +search full-text search
            → Quality filter (recency / type / title / organizer boost)
            → LLM semantic reranking (0-10 score)
            → LLM deduplication (keep newest across knowledge spaces)
            → Document enrichment (conclusions / open questions)
  → Two-step LLM context synthesis (SELECT key sentences → SYNTHESIZE natural bullets + source labels)
  → Push briefing card to all attendees

Workflow 2 · In-meeting Q&A
User sends a question in bot DM (im.message event listener)
  → Load pre-meeting brief from context_store (TTL 4h)
  → Real-time wiki search (question as keyword)
       → Section-level extraction (split by heading → keyword scoring → top sections ≤ 3,000 chars)
  → Merge pre-meeting brief + live search results
  → LLM generates natural-language reply (conclusion → inline sources → closing)

Workflow 3 · Post-meeting Processing
Feishu vc.meeting.end event (WebSocket listener)
  → Fetch meeting transcript / minutes
  → LLM Action Item extraction (structured JSON)
  → Wiki linking (match relevant docs per Action Item)
  → Auto-create Feishu tasks assigned to owners
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| LLM | Doubao 2.0 (Volcengine Ark, OpenAI-compatible) |
| Feishu APIs | lark-cli (Calendar / IM / Wiki / Task / VC) |
| Service | FastAPI + APScheduler |
| Runtime | Python 3.11+, uvicorn |

## Quick Start

**1. Install dependencies**
```bash
pip install -e .
```

**2. Configure environment**
```bash
cp .env.example .env
# Edit .env and fill in the required fields:
# DOUBAO_API_KEY=your_api_key
# LLM_MODEL=your_endpoint_id
# WIKI_SPACE_ID=your_wiki_space_id
```

**3. Start the service**
```bash
uvicorn app.main:app --port 8080
```

**4. Debug endpoints**
```bash
# Manually trigger pre-meeting push (test keyword extraction + Wiki search + card push)
curl -X POST http://localhost:8080/debug/trigger-premeet \
  -H "Content-Type: application/json" \
  -d '{"event_id":"test-001","title":"Meeting Title","description":"Agenda","attendee_open_ids":["ou_xxx"]}'

# Manually trigger post-meeting pipeline (dry_run=true shows results without creating tasks)
curl -X POST http://localhost:8080/debug/trigger-postmeet \
  -H "Content-Type: application/json" \
  -d '{"meeting_id":"your_meeting_id","dry_run":true}'
```
