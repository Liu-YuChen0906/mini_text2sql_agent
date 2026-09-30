<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'

type Status = 'idle' | 'running' | 'pending' | 'interrupted' | 'closed'
type Message = {
  id: number
  turn_id: number
  role: 'user' | 'assistant'
  kind: 'text' | 'query' | 'error' | 'clarification' | 'sql_review' | 'clarification_answer' | 'review_action'
  payload: Record<string, any>
  created_at: string
}
type SessionSummary = { id: string; title: string; status: Status; updated_at: string }
type Session = SessionSummary & {
  turn_id: number
  pending: (Record<string, any> & { id: string; kind: string }) | null
  messages: Message[]
}

const sessions = ref<SessionSummary[]>([])
const current = ref<Session | null>(null)
const draft = ref('')
const answer = ref('')
const decision = ref('approve')
const feedback = ref('')
const busy = ref(false)
const workingSessionId = ref('')
const sidebarOpen = ref(false)
const error = ref('')
const bottom = ref<HTMLElement | null>(null)
let poll: ReturnType<typeof setInterval> | undefined

function newRequestId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

const currentId = computed(() => current.value?.id || '')
const statusLabel: Record<Status, string> = {
  idle: '就绪', running: '执行中', pending: '等待回复', interrupted: '执行中断', closed: '已结束',
}
const decisionLabels: Record<string, string> = {
  approve: '通过', edit_sql: '重写 SQL', edit_schema: '重新选表', reject: '拒绝', exit: '结束会话',
}

async function api<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`)
  return body as T
}

async function refreshList() {
  sessions.value = await api<SessionSummary[]>('/api/sessions')
}

async function showSession(id: string) {
  error.value = ''
  current.value = await api<Session>(`/api/sessions/${id}`)
  localStorage.setItem('mini-openchatbi-session', id)
  sidebarOpen.value = false
  answer.value = ''
  feedback.value = ''
  decision.value = 'approve'
  await scrollBottom()
}

async function refreshCurrent() {
  const id = currentId.value
  if (!id) return
  try {
    const state = await api<Session>(`/api/sessions/${id}`)
    if (currentId.value === id) current.value = state
    await refreshList()
  } catch (caught) {
    error.value = messageOf(caught)
  }
}

async function createSession() {
  try {
    const session = await api<Session>('/api/sessions', { method: 'POST' })
    current.value = session
    localStorage.setItem('mini-openchatbi-session', session.id)
    sidebarOpen.value = false
    error.value = ''
    await refreshList()
  } catch (caught) { error.value = messageOf(caught) }
}

async function submitMessage() {
  const id = currentId.value
  const content = draft.value.trim()
  if (!id || !content || current.value?.status !== 'idle' || busy.value) return
  busy.value = true
  workingSessionId.value = id
  error.value = ''
  draft.value = ''
  // Keep the same ID for this one attempt; a failed network response is reconciled by reading the session.
  const requestId = newRequestId()
  try {
    const request = api<Session>(`/api/sessions/${id}/messages`, {
      method: 'POST', body: JSON.stringify({ content, request_id: requestId }),
    })
    await refreshCurrent()
    const result = await request
    if (currentId.value === id) current.value = result
    await refreshList()
    await scrollBottom()
  } catch (caught) {
    error.value = messageOf(caught)
    await refreshCurrent()
  } finally { busy.value = false; workingSessionId.value = '' }
}

function onComposerKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    void submitMessage()
  }
}

async function submitPending() {
  const id = currentId.value
  const pending = current.value?.pending
  if (!id || !pending || busy.value) return
  const payload: Record<string, string> = {
    interaction_id: pending.id, request_id: newRequestId(),
  }
  if (pending.kind === 'clarification') {
    if (!answer.value.trim()) return
    payload.answer = answer.value.trim()
  } else {
    payload.decision = decision.value
    if (['edit_sql', 'edit_schema'].includes(decision.value)) {
      if (!feedback.value.trim()) return
      payload.feedback = feedback.value.trim()
    }
  }
  busy.value = true
  workingSessionId.value = id
  error.value = ''
  try {
    const request = api<Session>(`/api/sessions/${id}/resume`, {
      method: 'POST', body: JSON.stringify(payload),
    })
    await refreshCurrent()
    const result = await request
    if (currentId.value === id) current.value = result
    await refreshList()
    await scrollBottom()
  } catch (caught) {
    error.value = messageOf(caught)
    await refreshCurrent()
  } finally { busy.value = false; workingSessionId.value = '' }
}

async function retry() {
  const id = currentId.value
  if (!id || busy.value) return
  busy.value = true
  workingSessionId.value = id
  error.value = ''
  try {
    const request = api<Session>(`/api/sessions/${id}/retry`, { method: 'POST' })
    await refreshCurrent()
    const result = await request
    if (currentId.value === id) current.value = result
    await refreshList()
  } catch (caught) {
    error.value = messageOf(caught)
    await refreshCurrent()
  } finally { busy.value = false; workingSessionId.value = '' }
}

async function copySql(sql: string) {
  try { await navigator.clipboard.writeText(sql) }
  catch { error.value = '复制失败，请手动选择 SQL。' }
}

function messageOf(value: unknown) { return value instanceof Error ? value.message : '网络连接失败，请刷新会话状态。' }
function dateText(value: string) { return new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) }
function cellText(value: unknown) { return value === null ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : String(value) }
function isActiveInteraction(message: Message) {
  return current.value?.pending?.id === message.payload.interaction_id
}
async function scrollBottom() { await nextTick(); bottom.value?.scrollIntoView({ behavior: 'smooth' }) }

onMounted(async () => {
  try {
    await refreshList()
    const saved = localStorage.getItem('mini-openchatbi-session')
    if (saved && sessions.value.some(item => item.id === saved)) await showSession(saved)
    else if (sessions.value.length) await showSession(sessions.value[0].id)
    poll = setInterval(() => {
      if (current.value?.status === 'running') void refreshCurrent()
    }, 1500)
  } catch (caught) { error.value = messageOf(caught) }
})
onUnmounted(() => { if (poll) clearInterval(poll) })
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="brand"><span class="brand-mark">◈</span><div><strong>Mini OpenChatBI</strong><small>数据对话工作台</small></div></div>
      <button class="new-button" @click="createSession"><span>＋</span> 新建会话</button>
      <div class="sidebar-heading">最近会话 <span>{{ sessions.length }}</span></div>
      <div class="session-list">
        <button v-for="session in sessions" :key="session.id" class="session-item" :class="{ selected: currentId === session.id }" @click="showSession(session.id)">
          <span class="session-title">{{ session.title }}</span>
          <span class="session-meta"><span>{{ dateText(session.updated_at) }}</span><span v-if="session.status !== 'idle'" class="small-status">{{ statusLabel[session.status] }}</span></span>
        </button>
      </div>
      <div class="sidebar-foot">本地单人使用 · 数据保存在本机</div>
    </aside>
    <div v-if="sidebarOpen" class="sidebar-shade" @click="sidebarOpen = false"></div>
    <main class="main-panel">
      <header class="topbar">
        <button class="menu-button" aria-label="切换侧栏" @click="sidebarOpen = !sidebarOpen">☰</button>
        <div class="topbar-title"><strong>{{ current?.title || 'Mini OpenChatBI' }}</strong><span v-if="current" class="status-pill" :class="current.status">{{ statusLabel[current.status] }}</span></div>
        <button class="subtle-button" @click="refreshCurrent" :disabled="!current">刷新</button>
      </header>
      <div v-if="error" class="error-banner" role="alert">{{ error }} <button @click="error = ''">关闭</button></div>
      <div class="conversation">
        <div v-if="!current || !current.messages.length" class="welcome">
          <div class="welcome-icon">◈</div>
          <h1>从一个问题开始</h1>
          <p>询问业务数据，查看 SQL 和结果；需要补充信息或人工审核时，在这里继续处理。</p>
          <button v-if="!current" class="new-button" @click="createSession">新建会话</button>
        </div>
        <div v-for="item in current?.messages || []" :key="item.id" class="message-row" :class="item.role">
          <div class="avatar">{{ item.role === 'user' ? '我' : '◈' }}</div>
          <div class="message-content">
            <div class="message-author">{{ item.role === 'user' ? '你' : 'Mini OpenChatBI' }} <time>{{ dateText(item.created_at) }}</time></div>
            <div v-if="item.kind === 'text'" class="text-message">{{ item.payload.content }}</div>
            <div v-else-if="item.kind === 'query'" class="result-card">
              <div class="card-heading"><strong>查询结果</strong><span v-if="item.payload.human_decision === 'approve'" class="approved">人工已通过</span></div>
              <div class="sql-heading"><span>SQL</span><button @click="copySql(item.payload.sql)">复制 SQL</button></div>
              <pre class="sql-block">{{ item.payload.sql }}</pre>
              <div class="table-wrap" v-if="item.payload.rows?.length"><table><thead><tr><th v-for="(column, index) in item.payload.columns" :key="index">{{ column }}</th></tr></thead><tbody><tr v-for="(row, index) in item.payload.rows" :key="index"><td v-for="(value, cellIndex) in row" :key="cellIndex" :class="{ null: value === null }">{{ cellText(value) }}</td></tr></tbody></table></div>
              <p v-else class="empty-result">没有查到数据。</p>
              <div class="result-footer"><span>{{ item.payload.rows?.length || 0 }} 行<span v-if="item.payload.truncated"> · 仅显示前 100 行，后面还有数据</span></span><span>模型评分：{{ typeof item.payload.confidence === 'number' ? item.payload.confidence.toFixed(2) : '不可用' }}</span></div>
              <p v-if="item.payload.reasons?.length" class="score-reasons">{{ item.payload.reasons.join('；') }}</p>
            </div>
            <div v-else-if="item.kind === 'error'" class="result-card failure"><strong>查询未完成</strong><p>{{ item.payload.message }}</p><pre v-if="item.payload.sql" class="sql-block">{{ item.payload.sql }}</pre></div>
            <div v-else-if="item.kind === 'clarification'" class="result-card interaction">
              <strong>需要补充信息</strong><p>{{ item.payload.question }}</p>
              <template v-if="isActiveInteraction(item)"><textarea v-model="answer" rows="2" placeholder="请输入补充回答"></textarea><button class="primary-button" :disabled="busy || !answer.trim()" @click="submitPending">提交回答</button></template>
              <small v-else>已处理</small>
            </div>
            <div v-else-if="item.kind === 'sql_review'" class="result-card interaction">
              <strong>SQL 需要人工审核</strong><p>{{ item.payload.question }}</p>
              <div class="sql-heading"><span>待审核 SQL</span><button @click="copySql(item.payload.sql)">复制 SQL</button></div><pre class="sql-block">{{ item.payload.sql }}</pre>
              <div class="review-meta">模型评分：{{ typeof item.payload.confidence === 'number' ? item.payload.confidence.toFixed(2) : '不可用' }}<span v-if="item.payload.reasons?.length"> · {{ item.payload.reasons.join('；') }}</span></div>
              <div class="table-wrap" v-if="item.payload.result_preview?.length"><table><thead><tr><th v-for="(column, index) in item.payload.columns" :key="index">{{ column }}</th></tr></thead><tbody><tr v-for="(row, index) in item.payload.result_preview" :key="index"><td v-for="(value, cellIndex) in row" :key="cellIndex">{{ cellText(value) }}</td></tr></tbody></table></div><p v-else class="empty-result">预览结果为空。</p>
              <template v-if="isActiveInteraction(item)"><label class="field-label">审核决定</label><select v-model="decision"><option v-for="option in item.payload.options" :key="option" :value="option">{{ decisionLabels[option] }}</option></select><textarea v-if="decision === 'edit_sql' || decision === 'edit_schema'" v-model="feedback" rows="2" placeholder="请用自然语言说明需要修改的内容"></textarea><button class="primary-button" :disabled="busy || (['edit_sql', 'edit_schema'].includes(decision) && !feedback.trim())" @click="submitPending">提交审核</button></template>
              <small v-else>已处理</small>
            </div>
            <div v-else-if="item.kind === 'clarification_answer'" class="text-message">补充回答：{{ item.payload.answer }}</div>
            <div v-else-if="item.kind === 'review_action'" class="text-message">审核：{{ decisionLabels[item.payload.decision] }}<span v-if="item.payload.feedback"> · {{ item.payload.feedback }}</span></div>
          </div>
        </div>
        <div v-if="current?.status === 'running' || workingSessionId === currentId" class="working"><span class="spinner"></span> 正在生成完整结果…</div>
        <div v-if="current?.status === 'interrupted'" class="interrupted-card"><strong>执行中断</strong><p>服务上次执行未完成。请确认后从已保存的检查点继续。</p><button class="primary-button" :disabled="busy" @click="retry">重试执行</button></div>
        <div ref="bottom"></div>
      </div>
      <footer class="composer-wrap">
        <div class="composer">
          <textarea
            v-model="draft"
            rows="2"
            :disabled="!current || current.status !== 'idle' || busy"
            :placeholder="current?.status === 'pending' ? '请先处理上方的澄清或审核' : current?.status === 'closed' ? '会话已结束' : '输入问题，按 Enter 发送…'"
            @keydown="onComposerKeydown"
          ></textarea>
          <button
            class="send-button"
            aria-label="发送消息"
            :disabled="!draft.trim() || !current || current.status !== 'idle' || busy"
            @click="submitMessage"
          >↑</button>
        </div>
        <div class="composer-hint">Enter 发送 · Shift + Enter 换行</div>
      </footer>
    </main>
  </div>
</template>
