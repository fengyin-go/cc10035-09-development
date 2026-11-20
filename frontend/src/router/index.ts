import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import SemanticEvidenceView from '@/views/SemanticEvidenceView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径优先于 :moduleKey 动态路由，避免被通用工作台截获。
    { path: '/semantic_evidence', name: 'semantic-evidence', component: SemanticEvidenceView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
