import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from './components/AppLayout.vue'
import QueuePage from './pages/QueuePage.vue'

const routes = [
  {
    path: '/',
    component: AppLayout,
    children: [
      { path: '', component: QueuePage },
      { path: 'vnext', component: () => import('./pages/VnextPage.vue') },
      { path: 'vnext/:bookId', component: () => import('./pages/VnextDetailPage.vue') },
      { path: 'settings', component: () => import('./pages/SettingsPage.vue') },
      { path: 'characters', component: () => import('./pages/CharacterCardPage.vue') },
      { path: 'summary', component: () => import('./pages/SummaryPage.vue') },
      { path: 'splitter', component: () => import('./pages/SplitterPage.vue') },
      { path: 'workspace', component: () => import('./pages/WorkspacePage.vue') },
      { path: 'prompt', component: () => import('./pages/PromptPreviewPage.vue') },
      { path: 'style', component: () => import('./pages/StylePage.vue') },
      { path: 'stats', component: () => import('./pages/StatsPage.vue') },
      { path: 'timeline', component: () => import('./pages/TimelinePage.vue') },
      { path: 'graph', component: () => import('./pages/GraphPage.vue') },
      { path: 'map', component: () => import('./pages/MapPage.vue') },
    ],
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
