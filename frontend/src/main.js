import '@tabler/core/dist/css/tabler.min.css'
import '@tabler/core/dist/js/tabler.min.js'
import './style.css'

const app = document.querySelector('#app')

const state = {
  status: null,
  config: null,
  busy: false,
  lastRefresh: null,
  adminToken: '',
  authStep: 'idle',
  pollTimer: null,
  refreshInFlight: false,
  configDirty: false,
  configNotice: '',
}

app.innerHTML = `
  <div class="page">
    <header class="navbar navbar-expand-md d-print-none" data-bs-theme="dark">
      <div class="container-xl">
        <h1 class="navbar-brand navbar-brand-autodark pe-0 pe-md-3">
          <span class="avatar avatar-sm bg-primary text-white me-2">B</span>
          Bark Monitor
        </h1>
        <div class="navbar-nav flex-row order-md-last">
          <div class="nav-item d-flex align-items-center gap-2">
            <label class="visually-hidden" for="admin-token">管理令牌</label>
            <input id="admin-token" class="form-control form-control-sm token-input" type="password" autocomplete="off" placeholder="管理令牌" />
            <button id="refresh-button" class="btn btn-outline-light btn-sm" type="button" title="刷新状态">
              <span class="icon-refresh" aria-hidden="true">↻</span><span class="d-none d-sm-inline ms-1">刷新</span>
            </button>
          </div>
        </div>
      </div>
    </header>

    <div class="page-wrapper">
      <div class="container-xl">
        <div id="alert-region" class="pt-3" aria-live="polite"></div>
        <div class="page-header d-print-none py-3">
          <div class="row align-items-center">
            <div class="col">
              <h2 class="page-title">本地监听控制台</h2>
              <div class="text-secondary">查看 Telegram 连接、Bark 推送和运行状态</div>
            </div>
            <div class="col-auto">
              <span id="overall-badge" class="badge bg-secondary-lt">正在读取状态</span>
            </div>
          </div>
        </div>

        <div class="row row-cards mb-3">
          <div class="col-sm-6 col-lg-3">
            <div class="card card-sm">
              <div class="card-body">
                <div class="row align-items-center">
                  <div class="col-auto"><span class="avatar bg-primary-lt">◉</span></div>
                  <div class="col"><div class="font-weight-medium">监听状态</div><div id="monitor-state" class="text-secondary">读取中</div></div>
                </div>
              </div>
            </div>
          </div>
          <div class="col-sm-6 col-lg-3">
            <div class="card card-sm">
              <div class="card-body">
                <div class="row align-items-center">
                  <div class="col-auto"><span class="avatar bg-azure-lt">↔</span></div>
                  <div class="col"><div class="font-weight-medium">Telegram</div><div id="telegram-state" class="text-secondary">读取中</div></div>
                </div>
              </div>
            </div>
          </div>
          <div class="col-sm-6 col-lg-3">
            <div class="card card-sm">
              <div class="card-body">
                <div class="row align-items-center">
                  <div class="col-auto"><span class="avatar bg-green-lt">⌁</span></div>
                  <div class="col"><div class="font-weight-medium">已推送</div><div id="delivered-count" class="text-secondary">—</div></div>
                </div>
              </div>
            </div>
          </div>
          <div class="col-sm-6 col-lg-3">
            <div class="card card-sm">
              <div class="card-body">
                <div class="row align-items-center">
                  <div class="col-auto"><span class="avatar bg-red-lt">!</span></div>
                  <div class="col"><div class="font-weight-medium">失败</div><div id="failed-count" class="text-secondary">—</div></div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="row row-cards">
          <div class="col-lg-7">
            <section class="card mb-3" aria-labelledby="control-title">
              <div class="card-header"><h3 id="control-title" class="card-title">监听控制</h3></div>
              <div class="card-body">
                <div class="d-flex flex-wrap gap-2 mb-3">
                  <button id="start-button" class="btn btn-primary" type="button">启动监听</button>
                  <button id="stop-button" class="btn btn-outline-danger" type="button">停止监听</button>
                  <button id="test-button" class="btn btn-outline-secondary" type="button">发送测试 Bark</button>
                </div>
                <dl class="row mb-0 compact-details">
                  <dt class="col-sm-4">最近刷新</dt><dd id="last-refresh" class="col-sm-8">—</dd>
                  <dt class="col-sm-4">最近收到消息</dt><dd id="last-event" class="col-sm-8">—</dd>
                  <dt class="col-sm-4">最近错误</dt><dd id="last-error" class="col-sm-8 text-danger">—</dd>
                </dl>
              </div>
            </section>

            <section id="auth-card" class="card mb-3 d-none" aria-labelledby="auth-title">
              <div class="card-header"><h3 id="auth-title" class="card-title">Telegram 授权</h3></div>
              <div class="card-body">
                <p class="text-secondary small">首次授权需要验证码；如果账号开启了二步验证，随后再输入二步验证密码。密码只在本次请求中使用，不会保存到浏览器。</p>
                <div id="auth-phone-row" class="row g-2 align-items-end">
                  <div class="col-sm-8"><label class="form-label" for="auth-phone">手机号</label><input id="auth-phone" class="form-control" type="tel" autocomplete="tel" placeholder="例如 +8613812345678" /></div>
                  <div class="col-sm-4"><button id="request-code-button" class="btn btn-outline-primary w-100" type="button">发送验证码</button></div>
                </div>
                <div id="auth-code-row" class="row g-2 align-items-end mt-2 d-none">
                  <div class="col-sm-8"><label class="form-label" for="auth-code">Telegram 验证码</label><input id="auth-code" class="form-control" type="text" inputmode="numeric" autocomplete="one-time-code" /></div>
                  <div class="col-sm-4"><button id="sign-in-button" class="btn btn-primary w-100" type="button">验证登录</button></div>
                </div>
                <div id="auth-password-row" class="row g-2 align-items-end mt-2 d-none">
                  <div class="col-sm-8"><label class="form-label" for="auth-password">二步验证密码</label><input id="auth-password" class="form-control" type="password" autocomplete="current-password" /></div>
                  <div class="col-sm-4"><button id="password-button" class="btn btn-primary w-100" type="button">完成授权</button></div>
                </div>
              </div>
            </section>

            <section class="card mb-3" aria-labelledby="activity-title">
              <div class="card-header"><h3 id="activity-title" class="card-title">运行摘要</h3></div>
              <div class="card-body">
                <div class="row g-3">
                  <div class="col-sm-4"><div class="text-secondary small">收到消息</div><div id="received-count" class="h2 mb-0">—</div></div>
                  <div class="col-sm-4"><div class="text-secondary small">已推送</div><div id="summary-delivered-count" class="h2 mb-0">—</div></div>
                  <div class="col-sm-4"><div class="text-secondary small">当前配置</div><div id="config-state" class="h2 mb-0">读取中</div></div>
                </div>
              </div>
            </section>

            <section class="card mb-3" aria-labelledby="logs-title">
              <div class="card-header d-flex align-items-center justify-content-between">
                <h3 id="logs-title" class="card-title mb-0">运行日志</h3>
                <span class="text-secondary small">仅显示安全摘要</span>
              </div>
              <div class="list-group list-group-flush" id="logs-list" aria-live="polite">
                <div class="list-group-item text-secondary">读取中…</div>
              </div>
            </section>
          </div>

          <div class="col-lg-5">
            <section class="card mb-3" aria-labelledby="config-title">
              <div class="card-header"><h3 id="config-title" class="card-title">运行配置</h3></div>
              <form id="config-form" class="card-body">
                <p class="text-secondary small">敏感字段只用于提交，不会从接口回填。留空表示保留已有值。</p>
                <div class="mb-3"><label class="form-label" for="telegram-api-id">Telegram API ID</label><input id="telegram-api-id" class="form-control" name="TELEGRAM_API_ID" type="number" min="1" inputmode="numeric" required /></div>
                <div class="mb-3"><label class="form-label" for="telegram-api-hash">Telegram API Hash <span class="text-secondary">（敏感）</span></label><input id="telegram-api-hash" class="form-control" name="TELEGRAM_API_HASH" type="password" autocomplete="new-password" placeholder="已配置时留空" /></div>
                <div class="mb-3"><label class="form-label" for="telegram-phone">Telegram 手机号 <span class="text-secondary">（敏感）</span></label><input id="telegram-phone" class="form-control" name="TELEGRAM_PHONE" type="tel" autocomplete="tel" placeholder="已配置时留空" /></div>
                <div class="mb-3"><label class="form-label" for="bark-push-url">Bark 推送 URL <span class="text-secondary">（敏感）</span></label><input id="bark-push-url" class="form-control" name="BARK_PUSH_URL" type="password" autocomplete="new-password" placeholder="已配置时留空" /></div>
                <div class="mb-3"><label class="form-label" for="telegram-session">Session 文件</label><input id="telegram-session" class="form-control" name="TELEGRAM_SESSION" type="text" /></div>
                <div class="row g-2 mb-3">
                  <div class="col-sm-7"><label class="form-label" for="app-timezone">时区</label><input id="app-timezone" class="form-control" name="APP_TIMEZONE" type="text" /></div>
                  <div class="col-sm-5"><label class="form-label" for="bark-timeout">Bark 超时（秒）</label><input id="bark-timeout" class="form-control" name="BARK_TIMEOUT_SECONDS" type="number" min="0.1" step="0.1" /></div>
                </div>
                <div class="d-flex align-items-center justify-content-between gap-2">
                  <div>
                    <div id="config-hint" class="text-secondary small"></div>
                  </div>
                  <button id="save-config-button" class="btn btn-primary" type="submit">保存配置</button>
                </div>
              </form>
            </section>
          </div>
        </div>
      </div>
      <footer class="footer footer-transparent d-print-none"><div class="container-xl"><div class="text-secondary small">Bark Monitor · 本地管理面板</div></div></footer>
    </div>
  </div>
`

const $ = (selector) => document.querySelector(selector)

function showAlert(message, kind = 'info') {
  const allowed = new Set(['info', 'success', 'warning', 'danger'])
  const tone = allowed.has(kind) ? kind : 'info'
  $('#alert-region').innerHTML = `
    <div class="alert alert-${tone} alert-dismissible" role="alert">
      <div>${escapeHtml(message)}</div>
      <button class="btn-close" data-bs-dismiss="alert" aria-label="关闭"></button>
    </div>`
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}

function adminHeaders() {
  const token = state.adminToken.trim()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...adminHeaders(), ...(options.headers || {}) }
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json'
  const response = await fetch(path, { ...options, headers })
  const contentType = response.headers.get('content-type') || ''
  let payload
  try {
    payload = contentType.includes('application/json') ? await response.json() : await response.text()
  } catch {
    payload = null
  }
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' ? payload.detail || payload.message || payload.error : null
    const message = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `请求失败（HTTP ${response.status}），请检查服务或管理令牌`
    const error = new Error(message)
    error.status = response.status
    error.code = payload?.code
    throw error
  }
  if (!contentType.includes('application/json')) throw new Error('服务未返回 JSON，请检查后端连接或代理配置')
  return payload
}

function firstValue(object, ...keys) {
  for (const key of keys) {
    if (object && object[key] !== undefined && object[key] !== null) return object[key]
  }
  return undefined
}

function boolValue(object, ...keys) {
  const value = firstValue(object, ...keys)
  if (typeof value === 'string') return ['true', '1', 'yes', 'online', 'connected', 'running'].includes(value.toLowerCase())
  return Boolean(value)
}

function normalizeStatus(payload) {
  const value = payload && typeof payload === 'object' ? payload : {}
  const stats = firstValue(value, 'stats', 'statistics', 'metrics') || {}
  const monitorState = firstValue(value, 'status', 'state', 'monitor_status') || (boolValue(value, 'listening', 'running') ? 'running' : 'stopped')
  return {
    raw: value,
    state: String(monitorState).toLowerCase(),
    connected: boolValue(value, 'telegram_connected', 'connected'),
    authorized: boolValue(value, 'telegram_authorized', 'authorized'),
    authState: firstValue(value, 'auth_state'),
    listening: boolValue(value, 'listening', 'running') || ['running', 'listening', 'started', '运行中'].includes(String(monitorState).toLowerCase()),
    configured: firstValue(value, 'configured', 'config_valid'),
    received: firstValue(stats, 'received', 'received_count') ?? firstValue(value, 'received', 'received_count'),
    delivered: firstValue(stats, 'delivered', 'delivered_count', 'sent') ?? firstValue(value, 'delivered', 'delivered_count', 'sent'),
    failed: firstValue(stats, 'failed', 'failed_count', 'errors') ?? firstValue(value, 'failed', 'failed_count', 'errors'),
    lastEvent: firstValue(value, 'last_event_at', 'last_message_at', 'last_received_at'),
    lastError: firstValue(value, 'last_error', 'error'),
  }
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN')
}

function setText(selector, value, fallback = '—') {
  $(selector).textContent = value === undefined || value === null || value === '' ? fallback : String(value)
}

function renderStatus(payload) {
  const status = normalizeStatus(payload)
  state.status = status
  const running = status.listening
  const authText = status.authState || (status.authorized ? '已授权' : status.connected ? '已连接，待授权' : '未连接')
  setText('#monitor-state', firstValue(payload, 'status') || (running ? '运行中' : '已停止'))
  setText('#telegram-state', authText)
  setText('#delivered-count', status.delivered)
  setText('#summary-delivered-count', status.delivered)
  setText('#failed-count', status.failed)
  setText('#received-count', status.received)
  setText('#last-event', formatDate(status.lastEvent))
  setText('#last-error', status.lastError || '—')
  $('#last-error').classList.toggle('text-danger', Boolean(status.lastError))
  setText('#last-refresh', formatDate(state.lastRefresh))
  setText('#config-state', status.configured === false ? '待配置' : '可用')
  const statusLabel = String(firstValue(payload, 'status') || (running ? '运行中' : '已停止'))
  const statusTone = statusLabel === '错误' ? 'bg-red-lt' : running ? 'bg-green-lt' : status.authorized ? 'bg-blue-lt' : 'bg-yellow-lt'
  $('#overall-badge').className = `badge ${statusTone}`
  $('#overall-badge').textContent = statusLabel
  $('#auth-card').classList.toggle('d-none', status.authorized)
  $('#auth-code-row').classList.toggle('d-none', !['等待验证码', '等待二步验证密码'].includes(status.authState))
  $('#auth-password-row').classList.toggle('d-none', status.authState !== '等待二步验证密码')
  syncControlState()
}

function renderConfig(payload) {
  const config = payload && typeof payload === 'object' ? payload : {}
  state.config = config
  const apiId = firstValue(config, 'telegram_api_id', 'TELEGRAM_API_ID', 'api_id')
  const session = firstValue(config, 'telegram_session', 'TELEGRAM_SESSION', 'session_name')
  const timezone = firstValue(config, 'app_timezone', 'APP_TIMEZONE', 'timezone_name')
  const timeout = firstValue(config, 'bark_timeout_seconds', 'BARK_TIMEOUT_SECONDS', 'timeout_seconds')
  // 轮询只更新配置状态，避免覆盖用户正在编辑的表单。
  if (!state.configDirty) {
    if (apiId !== undefined) $('#telegram-api-id').value = apiId
    if (session !== undefined) $('#telegram-session').value = session
    if (timezone !== undefined) $('#app-timezone').value = timezone
    if (timeout !== undefined) $('#bark-timeout').value = timeout
  }
  const hashSet = boolValue(config, 'telegram_api_hash_set', 'api_hash_set')
  const phoneSet = boolValue(config, 'telegram_phone_set', 'phone_set')
  const barkSet = boolValue(config, 'bark_push_url_set', 'bark_url_set', 'push_url_set')
  $('#telegram-api-hash').placeholder = hashSet ? '已配置，留空表示保持不变' : '请输入 API Hash'
  $('#telegram-phone').placeholder = phoneSet ? '已配置，留空表示保持不变' : '请输入手机号'
  $('#bark-push-url').placeholder = barkSet ? '已配置，留空表示保持不变' : '请输入 Bark 推送 URL'
  $('#config-hint').textContent = state.configNotice
}

function renderLogs(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : []
  const list = $('#logs-list')
  list.replaceChildren()
  if (!items.length) {
    const empty = document.createElement('div')
    empty.className = 'list-group-item text-secondary'
    empty.textContent = '暂无运行日志'
    list.append(empty)
    return
  }
  for (const item of items.slice(-50).reverse()) {
    const row = document.createElement('div')
    row.className = 'list-group-item'
    const header = document.createElement('div')
    header.className = 'd-flex align-items-center gap-2 mb-1 small'
    const badge = document.createElement('span')
    const level = String(item.level || 'INFO').toUpperCase()
    badge.className = `badge ${['ERROR', 'CRITICAL'].includes(level) ? 'bg-red-lt' : level === 'WARNING' ? 'bg-yellow-lt' : 'bg-secondary-lt'}`
    badge.textContent = level
    const timestamp = document.createElement('span')
    timestamp.className = 'text-secondary'
    timestamp.textContent = formatDate(item.timestamp)
    const message = document.createElement('div')
    message.className = 'log-message'
    message.textContent = String(item.message || '')
    header.append(badge, timestamp)
    row.append(header, message)
    list.append(row)
  }
}

async function refresh() {
  if (state.refreshInFlight) return
  state.refreshInFlight = true
  try {
    const [status, config, logs] = await Promise.all([api('/api/status'), api('/api/config'), api('/api/logs?limit=100')])
    state.lastRefresh = new Date().toISOString()
    renderStatus(status)
    renderConfig(config)
    renderLogs(logs)
  } catch (error) {
    setText('#last-refresh', new Date().toLocaleString('zh-CN'))
    showAlert(error.message, error.status === 401 ? 'warning' : 'danger')
  } finally {
    state.refreshInFlight = false
  }
}

function syncControlState() {
  const running = Boolean(state.status?.listening)
  const active = running || ['启动中', '停止中'].includes(state.status?.state)
  const controls = {
    '#start-button': state.busy || active,
    '#stop-button': state.busy || !active || state.status?.state === '停止中',
    '#test-button': state.busy,
    '#save-config-button': state.busy,
    '#request-code-button': state.busy,
    '#sign-in-button': state.busy,
    '#password-button': state.busy,
  }
  for (const [selector, disabled] of Object.entries(controls)) $(selector).disabled = disabled
}

function resolveFeedback(successMessage, result) {
  const feedback = typeof successMessage === 'function' ? successMessage(result) : successMessage
  if (feedback && typeof feedback === 'object') {
    return { message: String(feedback.message || ''), kind: feedback.kind || 'success' }
  }
  return { message: String(feedback || ''), kind: 'success' }
}

async function runAction(button, action, successMessage) {
  if (state.busy) return
  state.busy = true
  button.disabled = true
  if (state.status) renderStatus(state.status.raw)
  syncControlState()
  try {
    const result = await action()
    // 先使用动作响应，确保轮询正在进行时也能立即展示授权和启停状态。
    if (result && typeof result.status === 'string') renderStatus(result)
    const feedback = resolveFeedback(successMessage, result)
    showAlert(feedback.message, feedback.kind)
    await refresh()
    return result
  } catch (error) {
    showAlert(error.message, error.status === 401 ? 'warning' : 'danger')
  } finally {
    state.busy = false
    if (state.status) renderStatus(state.status.raw || state.status)
    syncControlState()
  }
}

function formPayload(form) {
  const payload = {}
  for (const field of form.elements) {
    if (!field.name || field.disabled) continue
    const value = field.value.trim()
    if (value || !['TELEGRAM_API_HASH', 'TELEGRAM_PHONE', 'BARK_PUSH_URL'].includes(field.name)) payload[field.name] = value
  }
  return payload
}

$('#admin-token').addEventListener('input', (event) => {
  state.adminToken = event.target.value
})

$('#config-form').addEventListener('input', () => {
  state.configDirty = true
  state.configNotice = ''
})

$('#refresh-button').addEventListener('click', refresh)

$('#start-button').addEventListener('click', (event) => runAction(event.currentTarget, () => api('/api/monitor/start', { method: 'POST' }), '监听已启动'))
$('#stop-button').addEventListener('click', (event) => runAction(event.currentTarget, () => api('/api/monitor/stop', { method: 'POST' }), '监听已停止'))
$('#test-button').addEventListener('click', (event) => runAction(
  event.currentTarget,
  () => api('/api/bark/test', { method: 'POST', body: JSON.stringify({ title: 'Bark Monitor 测试', body: '来自管理面板的测试通知' }) }),
  (result) => result?.accepted === false
    ? { message: 'Bark 服务未接受测试通知，请检查推送地址和日志。', kind: 'warning' }
    : '测试通知已接受',
))

$('#config-form').addEventListener('submit', async (event) => {
  event.preventDefault()
  const button = $('#save-config-button')
  const form = event.currentTarget
  const payload = formPayload(form)
  await runAction(button, async () => {
    const result = await api('/api/config', { method: 'PUT', body: JSON.stringify(payload) })
    state.configDirty = false
    state.configNotice = state.status?.listening
      ? '配置已保存；请停止后重新启动监听以使用新配置。'
      : '配置已保存。'
    return result
  }, '配置已保存')
  for (const name of ['TELEGRAM_API_HASH', 'TELEGRAM_PHONE', 'BARK_PUSH_URL']) {
    const field = form.elements[name]
    if (field) field.value = ''
  }
})

$('#request-code-button').addEventListener('click', (event) => runAction(event.currentTarget, async () => {
  const phone = $('#auth-phone').value.trim()
  const result = await api('/api/telegram/request-code', { method: 'POST', body: JSON.stringify(phone ? { phone } : {}) })
  if (result?.auth_state !== '已授权') $('#auth-code-row').classList.remove('d-none')
  return result
}, (result) => result?.auth_state === '已授权' ? 'Telegram 已经授权，无需重复发送验证码。' : '验证码请求已发送'))

$('#sign-in-button').addEventListener('click', (event) => runAction(event.currentTarget, async () => {
  const code = $('#auth-code').value.trim()
  if (!code) throw new Error('请输入 Telegram 验证码')
  try {
    const result = await api('/api/telegram/sign-in', { method: 'POST', body: JSON.stringify({ code }) })
    if (result?.auth_state === '等待二步验证密码' || result?.password_required) {
      $('#auth-password-row').classList.remove('d-none')
    }
    return result
  } catch (error) {
    if (error.status === 409 || error.code === 'password_required') {
      $('#auth-password-row').classList.remove('d-none')
      throw new Error('需要输入 Telegram 二步验证密码')
    }
    throw error
  } finally {
    $('#auth-code').value = ''
  }
}, (result) => result?.auth_state === '等待二步验证密码'
  ? '验证码正确，请输入 Telegram 二步验证密码。'
  : 'Telegram 授权成功'))

$('#password-button').addEventListener('click', (event) => runAction(event.currentTarget, async () => {
  const password = $('#auth-password').value
  if (!password) throw new Error('请输入二步验证密码')
  try {
    await api('/api/telegram/password', { method: 'POST', body: JSON.stringify({ password }) })
  } finally {
    $('#auth-password').value = ''
  }
}, 'Telegram 二步验证完成'))

state.pollTimer = window.setInterval(refresh, 10000)
refresh()
