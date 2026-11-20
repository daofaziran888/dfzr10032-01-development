<template>
  <section class="page" data-module="sampling">
    <header class="page-head">
      <div>
        <h2>数据源采样清单</h2>
        <p class="page-desc">
          按启用状态与采样周期编排采集轮次；同一来源的同一轮次只派发一次，接入地址、采样周期与截止分钟按同一来源版本成套生效。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="generate">
      <label class="filter-item">
        <span>轮次日期</span>
        <input v-model="roundForm.date" type="date" />
      </label>
      <label class="filter-item">
        <span>起始分钟</span>
        <input v-model="roundForm.start" type="time" />
      </label>
      <label class="filter-item">
        <span>截止分钟</span>
        <input v-model="roundForm.end" type="time" />
      </label>
      <button class="btn primary" type="submit">编排采集轮次</button>
    </form>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>轮次状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in itemStatuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>轮次</span>
        <input v-model="filters.round_key" placeholder="如 2026-11-20T12:20" />
      </label>
      <label class="filter-item">
        <span>来源 / 地址</span>
        <input v-model="filters.keyword" placeholder="按来源编号或接入地址检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <h3 class="section-title">采样清单</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>轮次</th>
          <th>来源编号</th>
          <th>来源版本</th>
          <th>接入地址</th>
          <th>采样周期</th>
          <th>截止分钟</th>
          <th>状态</th>
          <th>领用人</th>
          <th>派发编号</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['轮次'] }}</td>
          <td>{{ row['来源编号'] }}</td>
          <td>v{{ row['来源版本'] }}</td>
          <td class="mono">{{ row['接入地址'] }}</td>
          <td>{{ row['采样周期分钟'] }} 分钟</td>
          <td>{{ row['截止分钟'] }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row['领用人'] || '—' }}</td>
          <td>{{ row['派发编号'] || '—' }}</td>
          <td class="row-actions">
            <button
              v-if="row.status === '待领用'"
              class="link"
              type="button"
              @click="claim(row)"
            >
              领用
            </button>
            <button
              v-else-if="row.status === '已领用' && dispatchOf(row)"
              class="link"
              type="button"
              @click="confirmDispatch(dispatchOf(row)!.id)"
            >
              确认派发
            </button>
            <span v-else class="muted-text">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="10" class="empty-state">
            暂无采样轮次，可按启用来源的采样周期先编排一轮采集
          </td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">派发记录</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>派发编号</th>
          <th>轮次</th>
          <th>来源编号</th>
          <th>来源版本</th>
          <th>接入地址</th>
          <th>截止分钟</th>
          <th>状态</th>
          <th>操作人</th>
          <th>确认人</th>
          <th>作废原因</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in dispatches" :key="String(row.id)">
          <td>{{ row['派发编号'] }}</td>
          <td>{{ row['轮次'] }}</td>
          <td>{{ row['来源编号'] }}</td>
          <td>v{{ row['来源版本'] }}</td>
          <td class="mono">{{ row['接入地址'] }}</td>
          <td>{{ row['截止分钟'] }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row['操作人'] || '—' }}</td>
          <td>{{ row['确认人'] || '—' }}</td>
          <td>{{ row['作废原因'] || '—' }}</td>
        </tr>
        <tr v-if="!dispatches.length">
          <td :colspan="10" class="empty-state">暂无派发记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采样轮次 · {{ dispatches.length }} 条派发记录</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/sampling'
const itemStatuses = ['待领用', '已领用', '已派发', '已作废']

const rows = ref<Row[]>([])
const dispatches = ref<Row[]>([])
const total = ref(0)
const message = ref('')
const messageOk = ref(true)
const filters = ref<Record<string, string>>({ status: '', round_key: '', keyword: '' })
const roundForm = ref({ date: today(), start: '12:00', end: '12:30' })
const stats = ref([
  { label: '待领用轮次', value: 0 },
  { label: '已领用待确认', value: 0 },
  { label: '已派发轮次', value: 0 },
  { label: '有效派发记录', value: 0 },
])

function today(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

function dispatchOf(row: Row): Row | undefined {
  return dispatches.value.find((item) => item['派发编号'] === row['派发编号'])
}

function setMessage(text: string, ok: boolean) {
  message.value = text
  messageOk.value = ok
}

function resetFilters() {
  filters.value = { status: '', round_key: '', keyword: '' }
  void reload()
}

async function refreshSummary() {
  const response = await request(`${ENDPOINT}/summary`)
  if (!response.ok) return
  const payload = await response.json()
  stats.value = [
    { label: '待领用轮次', value: payload.pending ?? 0 },
    { label: '已领用待确认', value: payload.claimed ?? 0 },
    { label: '已派发轮次', value: payload.dispatched ?? 0 },
    { label: '有效派发记录', value: payload.valid_dispatches ?? 0 },
  ]
}

async function reload() {
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) query.set(key, value)
  })
  query.set('size', '200')
  try {
    const [itemsResponse, dispatchResponse] = await Promise.all([
      request(`${ENDPOINT}/items?${query.toString()}`),
      request(`${ENDPOINT}/dispatches?size=200`),
    ])
    if (!itemsResponse.ok || !dispatchResponse.ok) {
      throw new Error('采样清单读取失败')
    }
    const itemsPayload = await itemsResponse.json()
    const dispatchPayload = await dispatchResponse.json()
    rows.value = itemsPayload.items ?? []
    total.value = itemsPayload.total ?? rows.value.length
    dispatches.value = dispatchPayload.items ?? []
    await refreshSummary()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '采样清单读取失败', false)
  }
}

async function generate() {
  try {
    const response = await request(`${ENDPOINT}/rounds`, {
      method: 'POST',
      body: JSON.stringify({
        values: { start: roundForm.value.start, end: roundForm.value.end, date: roundForm.value.date },
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.detail || payload.message || '编排失败')
    }
    setMessage(payload.message, true)
    await reload()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '编排失败', false)
  }
}

async function claim(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/items/${row.id}/claim`, {
      method: 'POST',
      body: JSON.stringify({ values: { operator: '值班管理员' } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '领用失败')
    }
    setMessage(payload.message, true)
    await reload()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '领用失败', false)
  }
}

async function confirmDispatch(dispatchId: Row[string]) {
  try {
    const response = await request(`${ENDPOINT}/dispatches/${dispatchId}/confirm`, {
      method: 'POST',
      body: JSON.stringify({ values: { operator: '值班管理员' } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message || '确认失败')
    }
    setMessage(payload.message, true)
    await reload()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '确认失败', false)
  }
}

onMounted(reload)
</script>
