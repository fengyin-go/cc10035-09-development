import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 语义证据数据索引 ----------------------------------------------------

export type EvidenceSearchFilters = {
  page_uri?: string
  intent?: string
  sentiment_record_id?: string
  text_snippet?: string
  include_unconfirmed?: boolean
  page?: number
  size?: number
}

export type EvidenceSearchResult = {
  items: EvidenceEntry[]
  total: number
  page: number
  size: number
  filters: Record<string, string>
  blocked_unconfirmed: BlockedEvidence[]
}

export type EvidenceEntry = {
  id: number
  证据编号: string
  情感记录编号: string
  标注版本: string
  文本片段: string
  情感倾向: string
  意图标签: string
  语义记录编号: string
  预测分数: string
  页面出处: string
  状态: string
  历史编号: string[]
}

export type BlockedEvidence = {
  证据编号: string
  情感记录编号: string
  标注版本: string
  意图标签: string
  当前状态: string
  已确认出处: string
  请求页面: string
  原因: string
}

export type ChecklistItem = {
  id: number
  证据编号: string
  情感记录编号: string
  标注版本: string
  意图标签: string
  文本片段: string
  页面出处: string
  清单状态: string
}

export type EvidenceStats = {
  total_evidence: number
  confirmed_evidence: number
  pending_evidence: number
  covered_pages: number
  checklist_pending: number
  legacy_aliases: number
  page_breakdown: { 页面出处: string; 证据数: number; 意图分布: Record<string, number> }[]
}

async function evidencePost(path: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error('语义证据操作失败')
  return await response.json()
}

export async function searchEvidence(filters: EvidenceSearchFilters): Promise<EvidenceSearchResult> {
  const query = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== '' && value !== false) query.set(key, String(value))
  })
  const response = await request(`/api/semantic-evidence/search?${query.toString()}`)
  if (!response.ok) throw new Error('语义证据检索失败')
  return await response.json()
}

export async function loadEvidenceChecklist(): Promise<{ items: ChecklistItem[]; total: number }> {
  const response = await request('/api/semantic-evidence/checklist')
  if (!response.ok) throw new Error('标注交付清单读取失败')
  return await response.json()
}

export async function loadEvidenceStats(): Promise<EvidenceStats> {
  const response = await request('/api/semantic-evidence/stats')
  if (!response.ok) throw new Error('语义证据统计读取失败')
  return await response.json()
}

export function stageProvenance(payload: {
  annotation_version: string
  page_uri: string
  intent?: string
  text_snippet?: string
  sentiment_record_id?: string
}): Promise<Record<string, unknown>> {
  // 中断场景：只暂存不确认。
  return evidencePost('/api/semantic-evidence/supplement-provenance', { ...payload, commit: false })
}

export function commitProvenance(payload: {
  annotation_version: string
  page_uri: string
  intent?: string
  text_snippet?: string
  sentiment_record_id?: string
}): Promise<Record<string, unknown>> {
  return evidencePost('/api/semantic-evidence/supplement-provenance', { ...payload, commit: true })
}

export async function resolveLegacyId(legacyId: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/semantic-evidence/legacy/${encodeURIComponent(legacyId)}`)
  if (!response.ok) throw new Error('旧编号回查失败')
  return await response.json()
}
