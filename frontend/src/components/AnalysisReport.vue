<script setup lang="ts">
import EvidencePanel from './EvidencePanel.vue'
defineProps<{ report: Record<string, any>; sessionId: string }>()
const kinds: Record<string,string> = { fact: '事实', interpretation: '解释（需人工判断）', hypothesis: '假设（待验证）', recommendation: '建议' }
const verification: Record<string,string> = { verified: '规定计算校验通过', supported: '有真实执行支持', unverified: '未自动验证' }
</script>
<template>
  <article class="result-card analysis-report">
    <div class="card-heading"><strong>数据分析报告</strong><span>{{ report.status }}</span></div>
    <small>{{ report.report_id }}</small>
    <details><summary>统计口径与任务</summary><pre>{{ JSON.stringify(report.task, null, 2) }}</pre></details>
    <section v-for="claim in report.claims" :key="claim.claim_id" class="analysis-claim">
      <small>{{ kinds[claim.kind] }} · {{ verification[claim.verification_status] }}</small>
      <p>{{ claim.text }}</p>
      <p v-for="limit in claim.limitations" :key="limit">限制：{{ limit }}</p>
      <EvidencePanel v-for="id in claim.evidence_refs" :key="id" :session-id="sessionId" :evidence-id="id" />
    </section>
    <p v-if="!report.claims.length">没有可发布的已校验结论。</p>
    <details open><summary>限制与待解决问题</summary><p v-for="line in [...report.limitations, ...report.unresolved_questions]" :key="line">{{ line }}</p></details>
    <details v-if="report.validation_errors.length"><summary>未发布项的校验错误</summary><p v-for="line in report.validation_errors" :key="line">{{ line }}</p></details>
  </article>
</template>
<style scoped>
.analysis-claim { padding: 16px 0; border-top: 1px solid #e1e5ec; margin-top: 12px; }
small { color: #626f83; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
