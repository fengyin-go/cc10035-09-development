<template>
  <section class="page" data-module="semantic_evidence">
    <header class="page-head">
      <div>
        <h2>语义证据数据索引</h2>
        <p class="page-desc">
          按页面出处与意图反查支撑语义预测的原始文本片段。仅该页出处已确认的证据可被检索命中，
          待确认条目会被拦截并提示回情感标注核对，避免同文本跨页错用原文。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">全站证据数</span>
        <strong class="stat-value">{{ stats.total_evidence ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">出处已确认</span>
        <strong class="stat-value">{{ stats.confirmed_evidence ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待确认出处</span>
        <strong class="stat-value">{{ stats.pending_evidence ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">覆盖页面 / 待补清单</span>
        <strong class="stat-value">{{ stats.covered_pages ?? '—' }} / {{ stats.checklist_pending ?? '—' }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>页面出处</span>
        <input v-model="filters.page_uri" data-testid="filter-page" placeholder="按页面URI检索，只返回该页已确认证据" />
      </label>
      <label class="filter-item">
        <span>意图标签</span>
        <input v-model="filters.intent" data-testid="filter-intent" placeholder="如：投诉反馈" />
      </label>
      <label class="filter-item">
        <span>情感记录编号</span>
        <input v-model="filters.sentiment_record_id" data-testid="filter-sentiment" placeholder="按情感标注回查" />
      </label>
      <label class="filter-item">
        <span>文本片段</span>
        <input v-model="filters.text_snippet" data-testid="filter-snippet" placeholder="按原文片段关键词检索" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询证据</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <h3 class="section-title">命中证据（出处已在该页复核）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>证据编号</th>
          <th>页面出处</th>
          <th>意图标签</th>
          <th>原始文本片段</th>
          <th>情感记录编号</th>
          <th>标注版本</th>
          <th>历史编号回查</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in result.items" :key="String(row.id)" :data-testid="`evidence-row-${row.id}`">
          <td>{{ row.证据编号 }}</td>
          <td>{{ row.页面出处 }}</td>
          <td>{{ row.意图标签 }}</td>
          <td class="snippet-cell">{{ row.文本片段 }}</td>
          <td>
            <RouterLink :to="`/sentiment_tag?record=${encodeURIComponent(row.情感记录编号)}`">
              {{ row.情感记录编号 }}
            </RouterLink>
          </td>
          <td>{{ row.标注版本 }}</td>
          <td>
            <span v-for="alias in row.历史编号" :key="alias" class="alias-tag">{{ alias }}</span>
          </td>
          <td>{{ row.状态 }}</td>
        </tr>
        <tr v-if="!result.items.length">
          <td colspan="8" class="empty-state">当前条件下没有可在该页复核的证据</td>
        </tr>
      </tbody>
    </table>

    <section v-if="result.blocked_unconfirmed.length" class="blocked-panel" data-testid="blocked-panel">
      <h3 class="section-title">拦截记录（尚不能在请求页面复核，未返回原文）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>证据编号</th>
            <th>情感记录编号</th>
            <th>意图标签</th>
            <th>当前状态</th>
            <th>已确认出处</th>
            <th>请求页面</th>
            <th>拦截原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in result.blocked_unconfirmed" :key="`${item.证据编号}-${item.请求页面}`">
            <td>{{ item.证据编号 }}</td>
            <td>{{ item.情感记录编号 }}</td>
            <td>{{ item.意图标签 }}</td>
            <td>{{ item.当前状态 }}</td>
            <td>{{ item.已确认出处 || '—' }}</td>
            <td>{{ item.请求页面 }}</td>
            <td>{{ item.原因 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="checklist-panel">
      <h3 class="section-title">
        标注交付清单（待补出处 {{ checklist.total }} 条）
        <button class="btn ghost small" type="button" @click="loadChecklist">刷新清单</button>
      </h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>证据编号</th>
            <th>情感记录编号</th>
            <th>标注版本</th>
            <th>意图标签</th>
            <th>文本片段</th>
            <th>补交页面出处</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in checklist.items" :key="String(item.id)" :data-testid="`checklist-row-${item.id}`">
            <td>{{ item.证据编号 }}</td>
            <td>{{ item.情感记录编号 }}</td>
            <td>{{ item.标注版本 }}</td>
            <td>{{ item.意图标签 }}</td>
            <td class="snippet-cell">{{ item.文本片段 }}</td>
            <td>
              <input v-model="supplementPages[item.标注版本]" :data-testid="`page-input-${item.id}`"
                     placeholder="录入该证据确认的页面URI" />
            </td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`stage-${item.id}`"
                      @click="stageSupplement(item)">暂存（模拟中断）</button>
              <button class="link" type="button" :data-testid="`commit-${item.id}`"
                      @click="commitSupplement(item)">重投确认</button>
            </td>
          </tr>
          <tr v-if="!checklist.items.length">
            <td colspan="7" class="empty-state">交付清单已清空，所有证据出处均已确认</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="stats-panel">
      <h3 class="section-title">全站计数与逐页出处</h3>
      <table class="data-table">
        <thead>
          <tr><th>页面出处</th><th>证据数</th><th>意图分布</th></tr>
        </thead>
        <tbody>
          <tr v-for="page in stats.page_breakdown ?? []" :key="page.页面出处">
            <td>{{ page.页面出处 }}</td>
            <td>{{ page.证据数 }}</td>
            <td>{{ formatIntentDist(page.意图分布) }}</td>
          </tr>
          <tr v-if="!(stats.page_breakdown ?? []).length">
            <td colspan="3" class="empty-state">暂无已确认出处的证据</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="legacy-panel">
      <h3 class="section-title">历史编号回查</h3>
      <form class="filter-bar" @submit.prevent="lookupLegacy">
        <label class="filter-item">
          <span>旧预测/记录编号</span>
          <input v-model="legacyInput" data-testid="legacy-input" placeholder="输入历史预测记录编号" />
        </label>
        <button class="btn" data-testid="legacy-query" type="submit">回查出处</button>
      </form>
      <pre v-if="legacyResult" class="legacy-result" data-testid="legacy-result">{{ legacyResult }}</pre>
    </section>

    <footer class="page-foot">
      <span>共 {{ result.total }} 条可复核证据</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  commitProvenance,
  loadEvidenceChecklist,
  loadEvidenceStats,
  resolveLegacyId,
  searchEvidence,
  stageProvenance,
  type ChecklistItem,
  type EvidenceSearchResult,
  type EvidenceStats,
} from '@/api/backend-service'

const emptyFilters = () => ({
  page_uri: '',
  intent: '',
  sentiment_record_id: '',
  text_snippet: '',
})

const filters = reactive(emptyFilters())
const result = ref<EvidenceSearchResult>({
  items: [], total: 0, page: 1, size: 20, filters: {}, blocked_unconfirmed: [],
})
const checklist = ref<{ items: ChecklistItem[]; total: number }>({ items: [], total: 0 })
const stats = ref<Partial<EvidenceStats>>({})
const supplementPages = reactive<Record<string, string>>({})
const legacyInput = ref('')
const legacyResult = ref('')
const errorMessage = ref('')

function resetFilters() {
  Object.assign(filters, emptyFilters())
  void reload()
}

async function reload() {
  errorMessage.value = ''
  try {
    result.value = await searchEvidence({ ...filters })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '语义证据检索失败'
  }
}

async function loadChecklist() {
  try {
    checklist.value = await loadEvidenceChecklist()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付清单读取失败'
  }
}

async function loadStats() {
  try {
    stats.value = await loadEvidenceStats()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '证据统计读取失败'
  }
}

function supplementPayload(item: ChecklistItem) {
  return {
    annotation_version: item.标注版本,
    page_uri: supplementPages[item.标注版本] ?? '',
    intent: item.意图标签,
    text_snippet: item.文本片段,
    sentiment_record_id: item.情感记录编号,
  }
}

async function stageSupplement(item: ChecklistItem) {
  errorMessage.value = ''
  if (!supplementPages[item.标注版本]) {
    errorMessage.value = '请先录入补交的页面出处再暂存'
    return
  }
  try {
    await stageProvenance(supplementPayload(item))
    errorMessage.value = `已暂存（中断）：${item.证据编号} 的出处尚未确认，重投后才会生效`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '暂存失败'
  }
}

async function commitSupplement(item: ChecklistItem) {
  errorMessage.value = ''
  if (!supplementPages[item.标注版本]) {
    errorMessage.value = '请先录入补交的页面出处'
    return
  }
  try {
    await commitProvenance(supplementPayload(item))
    await Promise.all([reload(), loadChecklist(), loadStats()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补交出处失败'
  }
}

async function lookupLegacy() {
  errorMessage.value = ''
  legacyResult.value = ''
  if (!legacyInput.value.trim()) return
  try {
    const payload = await resolveLegacyId(legacyInput.value.trim())
    legacyResult.value = JSON.stringify(payload, null, 2)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '旧编号回查失败'
  }
}

function formatIntentDist(dist: Record<string, number>): string {
  return Object.entries(dist).map(([intent, count]) => `${intent}×${count}`).join('，')
}

onMounted(() => {
  void reload()
  void loadChecklist()
  void loadStats()
})
</script>

<style scoped>
.section-title {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 20px 0 8px;
  font-size: 15px;
}
.snippet-cell {
  max-width: 280px;
  white-space: pre-wrap;
  word-break: break-all;
}
.alias-tag {
  display: inline-block;
  margin: 1px 3px;
  padding: 1px 6px;
  border-radius: 10px;
  background: #eef2ff;
  color: #3548c2;
  font-size: 12px;
}
.blocked-panel {
  border-left: 3px solid #d97706;
  padding-left: 10px;
}
.btn.small {
  padding: 2px 10px;
  font-size: 12px;
}
.legacy-result {
  background: #0f172a;
  color: #d1fae5;
  padding: 12px;
  border-radius: 8px;
  font-size: 12px;
  overflow: auto;
}
</style>
