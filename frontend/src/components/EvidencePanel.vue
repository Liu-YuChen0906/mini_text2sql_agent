<script setup lang="ts">
import { ref, watch } from 'vue'
const props = defineProps<{ sessionId: string; evidenceId: string; depth?: number }>()
const record = ref<Record<string, any> | null>(null)
const error = ref('')
const datasets = ref<Record<string, any>[]>([])
const artifacts = ref<Record<string, any>[]>([])
const pages = ref<Record<string, number>>({})
const opened = ref(false)
const base = () => `/api/sessions/${encodeURIComponent(props.sessionId)}`
async function get(url: string) {
  const response = await fetch(url)
  if (!response.ok) throw new Error('证据不可用或内容校验失败')
  return response.json()
}
async function page(id: string, offset: number) {
  const data = await get(`${base()}/datasets/${encodeURIComponent(id)}?offset=${offset}&limit=20`)
  datasets.value = [...datasets.value.filter(d => d.metadata.dataset_id !== id), data]
  pages.value[id] = offset
}
async function load() {
  if (opened.value) { opened.value = false; return }
  opened.value = true
  record.value = null; datasets.value = []; artifacts.value = []; error.value = ''
  try {
    record.value = await get(`${base()}/evidence/${encodeURIComponent(props.evidenceId)}`)
    const ids = [...new Set<string>([...record.value!.input_dataset_ids, ...record.value!.output_dataset_ids])]
    for (const id of ids) await page(id, 0)
    const artifactIds = [...new Set<string>(record.value!.artifact_ids)]
    for (const id of artifactIds) {
      const response = await fetch(`${base()}/artifacts/${encodeURIComponent(id)}`)
      if (!response.ok) throw new Error('产物不可用或 hash 校验失败')
      const media = response.headers.get('content-type') || ''
      if (media.startsWith('text/plain')) artifacts.value.push({ id, text: await response.text() })
      else artifacts.value.push({ id, media })
    }
  } catch (caught) { error.value = caught instanceof Error ? caught.message : '读取失败' }
}
watch(() => props.evidenceId, () => { opened.value = false; record.value = null })
</script>

<template>
  <div class="evidence-panel">
    <button class="evidence-toggle" @click="load" :aria-expanded="opened">{{ opened ? '收起证据' : '展开证据' }} · {{ evidenceId }}</button>
    <div v-if="opened" class="evidence-body">
      <p v-if="error" role="alert">{{ error }}</p>
      <template v-if="record && !error">
        <p><strong>{{ record.tool_name }}</strong> · {{ record.status }} · {{ record.started_at }}</p>
        <p v-if="record.error">{{ record.error }}</p>
        <p v-for="warning in record.warnings" :key="warning" class="evidence-warning">{{ warning }}</p>
        <details open><summary>实际 SQL / 方法 / 参数</summary><pre>{{ JSON.stringify(record.parameters, null, 2) }}</pre></details>
        <details open><summary>结构化执行结果</summary><pre>{{ JSON.stringify(record.result, null, 2) }}</pre></details>
        <section v-for="dataset in datasets" :key="dataset.metadata.dataset_id">
          <strong>数据快照 · {{ dataset.metadata.row_count }} 行</strong>
          <pre>{{ JSON.stringify({ id: dataset.metadata.dataset_id, schema: dataset.metadata.columns, coverage: dataset.metadata.coverage, units: dataset.metadata.units, warnings: dataset.metadata.quality_warnings, source_truncated: dataset.metadata.source_truncated }, null, 2) }}</pre>
          <div class="table-wrap"><table><thead><tr><th v-for="col in dataset.columns" :key="col">{{ col }}</th></tr></thead><tbody><tr v-for="(row, i) in dataset.rows" :key="i"><td v-for="(cell, j) in row" :key="j">{{ cell === null ? 'NULL' : cell }}</td></tr></tbody></table></div>
          <button :disabled="!dataset.offset" @click="page(dataset.metadata.dataset_id, Math.max(0, dataset.offset - 20))">上一页</button>
          <button :disabled="dataset.offset + 20 >= dataset.metadata.row_count" @click="page(dataset.metadata.dataset_id, dataset.offset + 20)">下一页</button>
          <a :href="`${base()}/artifacts/${dataset.metadata.data_artifact_id}`" download>下载完整实际返回快照</a>
        </section>
        <section v-for="artifact in artifacts" :key="artifact.id">
          <a :href="`${base()}/artifacts/${artifact.id}`" download>下载产物 {{ artifact.id }}</a>
          <pre v-if="artifact.text !== undefined">{{ artifact.text }}</pre>
          <img v-if="artifact.media?.startsWith('image/png')" :src="`${base()}/artifacts/${artifact.id}`" alt="分析执行生成的图表" />
        </section>
        <details v-if="record.parent_evidence_ids.length && (depth || 0) < 10"><summary>回查父证据</summary>
          <EvidencePanel v-for="id in record.parent_evidence_ids" :key="id" :session-id="sessionId" :evidence-id="id" :depth="(depth || 0) + 1" />
        </details>
      </template>
    </div>
  </div>
</template>
<style scoped>
.evidence-panel { margin: 12px 0; border: 1px solid #d5dde6; border-radius: 8px; overflow: hidden; }
.evidence-toggle { padding: 10px; text-align: left; width: 100%; overflow-wrap: anywhere; }
.evidence-body { padding: 12px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; background: #f4f6f8; padding: 10px; max-height: 400px; overflow: auto; }
section { margin: 16px 0; }
a { display: inline-block; margin: 8px; }
img { max-width: 100%; }
.evidence-warning { color: #8a5300; }
</style>
