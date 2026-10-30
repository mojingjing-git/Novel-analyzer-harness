import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from './components/AppLayout.vue'

import QueuePage from './pages/QueuePage.vue'
import SettingsPage from './pages/SettingsPage.vue'
import CharacterCardPage from './pages/CharacterCardPage.vue'
import SummaryPage from './pages/SummaryPage.vue'
import SplitterPage from './pages/SplitterPage.vue'
import WorkspacePage from './pages/WorkspacePage.vue'
import PromptPreviewPage from './pages/PromptPreviewPage.vue'
import StylePage from './pages/StylePage.vue'
import StatsPage from './pages/StatsPage.vue'
import TimelinePage from './pages/TimelinePage.vue'
import GraphPage from './pages/GraphPage.vue'
import MapPage from './pages/MapPage.vue'
import VnextPage from './pages/VnextPage.vue'

const routes = [
  {
    path: '/',
    component: AppLayout,
    children: [
      { path: '', component: QueuePage },
      { path: 'vnext', component: VnextPage },
      { path: 'settings', component: SettingsPage },
      { path: 'characters', component: CharacterCardPage },
      { path: 'summary', component: SummaryPage },
      { path: 'splitter', component: SplitterPage },
      { path: 'workspace', component: WorkspacePage },
      { path: 'prompt', component: PromptPreviewPage },
      { path: 'style', component: StylePage },
      { path: 'stats', component: StatsPage },
      { path: 'timeline', component: TimelinePage },
      { path: 'graph', component: GraphPage },
      { path: 'map', component: MapPage },
    ],
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
