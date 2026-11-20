import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Render } from '../types'

// voxel.py escribe el progreso de cada render en <carpeta de trabajo>/.voxel/progreso/<video>.txt
// (o en VOXEL_PROGRESS_DIR); el mod lee la de la sesión.
const PROGRESS_SUBDIR = '.voxel/progreso'
// Un render terminado se sigue mostrando este tiempo y después se oculta.
const KEEP_MS = 20 * 60 * 1000

// Primera vez que el mod vio cada archivo: da el tiempo transcurrido aunque aún no llegue avance.
const firstSeen = new Map<string, number>()

const renders = atom({ plugin: 'voxel-progreso', key: 'renders' } as const, [] as Render[])

function parse(name: string, text: string, updatedMs: number): Render {
  const lines = text.split('\n')
  const r: Render = {
    name, done: 0, total: 0, chunks: 0, chunksDone: 0, gpu: '', where: '?', seconds: 0, rendered: 0, etaSeconds: 0,
    stage: 'preparando', checksOk: 0, checksFailed: 0, error: '', updatedMs,
  }
  for (const line of lines) {
    let m = line.match(/fotogramas (\d+)–(\d+) de \d+/)
    if (m) r.total = Number(m[2]) - Number(m[1])
    m = line.match(/modal: (\d+) fotogramas en (\d+) tramos sobre (\S+)/)
    if (m) {
      r.total = Number(m[1]); r.chunks = Number(m[2]); r.gpu = m[3]; r.where = 'modal'
      if (r.stage === 'preparando') r.stage = 'en cola'
    }
    m = line.match(/en vivo: (\d+)\/(\d+) fotogramas renderizados · (\d+) s/)
    if (m) { r.rendered = Number(m[1]); r.total = Number(m[2]); r.seconds = Number(m[3]); r.stage = 'renderizando' }
    m = line.match(/recibidos: (\d+)\/(\d+) fotogramas · (\d+) s/)
    if (m) { r.done = Number(m[1]); r.total = Number(m[2]); r.seconds = Math.max(r.seconds, Number(m[3])); r.stage = 'renderizando' }
    m = line.match(/(\d+)\/(\d+) fotogramas recibidos · (\d+) s/)
    if (m) { r.done = Number(m[1]); r.total = Number(m[2]); r.seconds = Number(m[3]); r.stage = 'renderizando' }
    if (/^\s*tramo \d+/.test(line)) r.chunksDone += 1
    if (/^\s*render .*: fotogramas/.test(line) && r.stage === 'preparando') r.stage = 'preparando'
    m = line.match(/render (\d+)\/(\d+) fotogramas · (\d+) s/)
    if (m) { r.done = Number(m[1]); r.total = Number(m[2]); r.seconds = Number(m[3]); r.where = 'local'; r.stage = 'renderizando' }
    if (/^\s*✓ /.test(line)) { r.checksOk += 1; r.stage = 'verificando' }
    if (/^\s*✗ /.test(line)) { r.checksFailed += 1; r.stage = 'verificando' }
    m = line.match(/^Error: (.*)/)
    if (m) r.error = m[1]
    m = line.match(/^exit=(\d+)/)
    if (m) r.stage = m[1] === '0' ? 'listo' : 'falló'
  }
  return r
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function scan($: any): Promise<Render[]> {
  const PROGRESS = `${await $.session.cwd()}/${PROGRESS_SUBDIR}`
  if (!(await $.fs.exists(PROGRESS))) return []
  const entries = await $.fs.list(PROGRESS)
  const now = await $.clock.now()
  const found: Render[] = []
  for (const entry of entries) {
    if (entry.kind !== 'file' || !entry.name.endsWith('.txt')) continue
    const text = await $.fs.read(`${PROGRESS}/${entry.name}`).catch(() => '')
    const r = parse(entry.name.replace(/\.txt$/, ''), text, entry.mtimeMs)
    const first = firstSeen.get(entry.name) ?? now
    firstSeen.set(entry.name, first)
    const live = Math.round((now - first) / 1000)
    const finished0 = r.stage === 'listo' || r.stage === 'falló'
    if (!finished0) r.seconds = Math.max(r.seconds, live)
    const progressFrames = Math.max(r.rendered, r.done)
    if (!finished0 && progressFrames > 0 && r.total > progressFrames) {
      r.etaSeconds = Math.round((r.seconds * (r.total - progressFrames)) / progressFrames)
    }
    const finished = r.stage === 'listo' || r.stage === 'falló'
    if (finished && now - entry.mtimeMs > KEEP_MS) continue
    found.push(r)
  }
  return found.sort((a, b) => a.name.localeCompare(b.name))
}

function bar(done: number, total: number, width = 18) {
  const filled = total ? Math.round((done / total) * width) : 0
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}

function clockText(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const refresh = async () => {
      const found = await scan($).catch(() => [] as Render[])
      const before = await read($, renders)
      if (JSON.stringify(before) !== JSON.stringify(found)) {
        for (const r of found) {
          const old = before.find(o => o.name === r.name)
          const ended = (r.stage === 'listo' || r.stage === 'falló') && old && old.stage !== r.stage
          if (ended) {
            $.ui.toast(r.stage === 'listo' ? `🎬 ${r.name}: render listo y verificado` : `🎬 ${r.name}: el render falló`)
          }
        }
        await update($, renders, () => found)
      }
    }
    await refresh()
    $.clock.every(2000, () => { void refresh() })
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, renders)
    if (e.props.hasSurvey || list.length === 0) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        {list.map(r => {
          const shown = Math.max(r.rendered, r.done)
          const pct = r.total ? Math.floor((shown / r.total) * 100) : 0
          const color = r.stage === 'listo' ? 'green' : r.stage === 'falló' ? 'red' : 'cyan'
          const where = r.where === 'modal' ? `Modal ${r.gpu}` : r.where === 'local' ? 'Mac' : ''
          const chunks = r.chunks ? ` · tramos ${r.chunksDone}/${r.chunks}` : ''
          const checks = r.checksOk + r.checksFailed
            ? ` · verificación ${r.checksOk}✓${r.checksFailed ? ` ${r.checksFailed}✗` : ''}` : ''
          return (
            <Box key={r.name}>
              <Text color={color}>🎬 {r.name.padEnd(11)} </Text>
              <Text color={color}>{bar(shown, r.total)}</Text>
              <Text> {String(pct).padStart(3)}% </Text>
              <Text dimColor>
                {shown}/{r.total || '?'} fotogramas{chunks} · {r.stage}
                {r.seconds ? ` · ${clockText(r.seconds)}` : ''}
                {r.etaSeconds ? ` · faltan ~${clockText(r.etaSeconds)}` : ''}{where ? ` · ${where}` : ''}{checks}
                {r.error ? ` · ${r.error.slice(0, 60)}` : ''}
              </Text>
            </Box>
          )
        })}
      </Box>
    )
  })
}
