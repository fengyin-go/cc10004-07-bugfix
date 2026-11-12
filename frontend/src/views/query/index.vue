<template>
  <section class="page" data-module="query">
    <header class="page-head">
      <div>
        <h2>交互查询管理</h2>
        <p class="page-desc">维护交互查询语句的输入、执行约束与结果记录，为数据查询提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记查询语句</button>
        <button class="btn" type="button" @click="exportRows">导出交互查询清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>语句编号</span>
        <input v-model="keyword" placeholder="按语句编号检索" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>轮次</th>
          <th>结果身份</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td><span class="status-badge" :class="statusClass(row)">{{ row.status ?? '—' }}</span></td>
          <td>{{ row.round ?? 0 }}</td>
          <td>{{ row['结果身份'] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!availableActions(row).length" class="muted-text">已裁定，不可回退</span>
            <button class="link" type="button" @click="openTrajectory(row)">执行轨迹</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无交互查询数据，可先登记查询语句</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="trajectory-panel">
      <header class="trajectory-head">
        <div>
          <h3>执行轨迹：{{ detail['语句编号'] }}</h3>
          <p class="page-desc">
            当前状态「{{ detail.status }}」，第 {{ detail.round ?? 0 }} 轮；
            <template v-if="detail['结果身份']">结果身份 {{ detail['结果身份'] }}；</template>
            <template v-if="detail['缓存策略']">已准入查询缓存 {{ detail['缓存策略'] }}。</template>
          </p>
        </div>
        <button class="btn ghost" type="button" @click="closeTrajectory">收起轨迹</button>
      </header>
      <ol class="trajectory-list">
        <li
          v-for="event in detail.trajectory ?? []"
          :key="event.seq"
          class="trajectory-item"
          :class="{ rejected: !event.accepted }"
        >
          <div class="trajectory-line">
            <span class="trajectory-seq">#{{ event.seq }}</span>
            <span class="trajectory-action">{{ event.action }}</span>
            <span class="trajectory-flow">{{ event.from ?? '—' }} → {{ event.to ?? '—' }}</span>
            <span class="trajectory-verdict">{{ event.accepted ? '已接受' : '未获接受' }}</span>
            <span class="trajectory-time">{{ event.at }}</span>
          </div>
          <p class="trajectory-reason">{{ event.reason }}</p>
          <p v-if="event.cache" class="trajectory-cache">
            缓存准入证据：策略 {{ event.cache['策略编号'] }}（条目 #{{ event.cache['缓存条目'] }}），
            结果身份 {{ event.cache['结果身份'] }}
          </p>
        </li>
      </ol>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条交互查询记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

interface CacheEvidence {
  策略编号?: string
  缓存条目?: number
  结果身份?: string
}

interface TrajectoryEvent {
  seq: number
  at: string
  action: string
  round: number
  from: string | null
  to: string | null
  accepted: boolean
  reason: string
  cache?: CacheEvidence
}

type Row = Record<string, unknown> & {
  id: number
  status?: string
  round?: number
  trajectory?: TrajectoryEvent[]
}

const ENDPOINT = '/api/query'
const columns = ["语句编号", "查询表达式", "目标数据集", "返回字段", "排序条件", "分页大小", "最大行数", "超时秒数"]
const statuses = ["草稿", "待执行", "执行中", "已完成", "已超时"]
// 与后端状态机一致：状态只能按 草稿→待执行→执行中→已完成/已超时 顺序流转
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  草稿: ["提交执行"],
  待执行: ["启动查询"],
  执行中: ["保存结果", "终止查询"],
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const detail = ref<Row | null>(null)

const stats = computed(() => [
  { label: '查询语句总数', value: total.value },
  { label: '已完成查询', value: rows.value.filter((row) => row.status === '已完成').length },
  { label: '执行中查询', value: rows.value.filter((row) => row.status === '执行中').length },
])

function availableActions(row: Row): string[] {
  return ACTIONS_BY_STATUS[String(row.status ?? '')] ?? []
}

function statusClass(row: Row): string {
  const status = String(row.status ?? '')
  if (status === '已完成') return 'done'
  if (status === '已超时') return 'timeout'
  if (status === '执行中') return 'running'
  return 'idle'
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '查询语句登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  let message = ''
  let failed = false
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      // 协议：动作与轮次放在 values 里提交，后端同时兼容顶层 action 的旧形态
      body: JSON.stringify({ values: { action, round: row.round ?? null } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      failed = true
      message = String(payload.message ?? payload.detail ?? '交互查询动作未生效')
    } else {
      message = String(payload.message ?? `查询语句已${action}`)
    }
  } catch (error) {
    failed = true
    message = error instanceof Error ? error.message : '交互查询操作失败'
  }
  // 无论动作是否被接受都刷新列表与轨迹：并发裁定可能已改变状态
  await reload()
  if (detail.value?.id === row.id) {
    await openTrajectory(row)
  }
  if (failed) {
    errorMessage.value = message
  } else {
    noticeMessage.value = message
  }
}

async function openTrajectory(row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('查询语句明细读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '查询语句明细读取失败'
  }
}

function closeTrajectory() {
  detail.value = null
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (keyword.value) params.set('keyword', keyword.value)
  if (statusFilter.value) params.set('status', statusFilter.value)
  params.set('size', '200')
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('查询语句列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交互查询列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.status-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; border: 1px solid var(--border); }
.status-badge.done { color: #067647; border-color: #067647; }
.status-badge.timeout { color: #b42318; border-color: #b42318; }
.status-badge.running { color: var(--brand); border-color: var(--brand); }
.status-badge.idle { color: var(--muted); }
.muted-text { color: var(--muted); font-size: 12px; margin-right: 6px; }
.notice-text { color: #067647; }
.filter-item select { padding: 4px 6px; border: 1px solid var(--border); border-radius: 6px; background: #fff; }
.trajectory-panel { margin-top: 12px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
.trajectory-head { display: flex; justify-content: space-between; align-items: flex-start; }
.trajectory-head h3 { margin: 0 0 4px; font-size: 15px; }
.trajectory-list { list-style: none; margin: 12px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.trajectory-item { border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.trajectory-item.rejected { border-color: #f1b8b2; background: #fef6f5; }
.trajectory-line { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.trajectory-seq { color: var(--muted); }
.trajectory-action { font-weight: 600; }
.trajectory-flow { color: var(--muted); }
.trajectory-item:not(.rejected) .trajectory-verdict { color: #067647; }
.trajectory-item.rejected .trajectory-verdict { color: #b42318; }
.trajectory-time { margin-left: auto; color: var(--muted); font-size: 12px; }
.trajectory-reason { margin: 6px 0 0; font-size: 12px; color: #344054; }
.trajectory-cache { margin: 4px 0 0; font-size: 12px; color: var(--brand); }
</style>
