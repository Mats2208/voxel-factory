export type Render = {
  name: string
  done: number
  total: number
  chunks: number
  chunksDone: number
  gpu: string
  where: 'modal' | 'local' | '?'
  seconds: number
  rendered: number
  etaSeconds: number
  stage: 'preparando' | 'en cola' | 'renderizando' | 'verificando' | 'listo' | 'falló'
  checksOk: number
  checksFailed: number
  error: string
  updatedMs: number
}

declare module 'claude-code' {
  interface PluginState {
    'voxel-progreso': { renders: Render[] }
  }
}
