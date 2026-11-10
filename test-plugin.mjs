/**
 * 自检: 用 mock ctx 真的加载 index.js, 走一遍工具注册和两次只读调用。
 *
 * 跑法: node test-plugin.mjs
 * 只跑只读的 screen_windows / screen_find —— 不会点鼠标、不动键盘。
 *
 * Python 解释器: DSH_SCREEN_KIT_PYTHON > python3 > python > py
 */
import { execFile, execFileSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

function findPython() {
  const explicit = process.env.DSH_SCREEN_KIT_PYTHON || process.env.DSH_PYTHON
  if (explicit) return explicit
  for (const candidate of ['python3', 'python', 'py']) {
    try {
      execFileSync(candidate, ['-c', 'import sys'], { stdio: 'ignore' })
      return candidate
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error('找不到 python3 / python / py; 用 DSH_SCREEN_KIT_PYTHON 指定路径')
}

const PYTHON = findPython()

const registered = new Map()

/** 把 ctx.subprocess.spawn 接到真的 child_process 上, 模拟 collected/done 这套接口。 */
function fakeSpawn({ argv, cwd, env, stdio }) {
  const child = execFile(
    argv[0],
    argv.slice(1),
    { cwd, env: { ...process.env, ...(env || {}) }, maxBuffer: 32 << 20, windowsHide: true },
    () => {},
  )
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', (d) => {
    stdout += d.toString('utf8')
  })
  child.stderr.on('data', (d) => {
    stderr += d.toString('utf8')
  })
  const done = new Promise((resolve) => {
    child.on('close', (code) => resolve({ exitCode: code ?? 0 }))
  })
  return {
    done,
    collected: {
      stdout: { readFrom: () => ({ text: stdout }) },
      stderr: { readFrom: () => ({ text: stderr }) },
    },
  }
}

const ctx = {
  effect(fn) {
    const dispose = fn()
    return () => {
      if (typeof dispose === 'function') dispose()
    }
  },
  on() {},
  tools: {
    register(def) {
      registered.set(def.name, def)
      return () => registered.delete(def.name)
    },
  },
  subprocess: {
    async resolveExecutable(name) {
      if (name === 'python') return PYTHON
      throw new Error('not found: ' + name)
    },
    spawn: fakeSpawn,
  },
}

const mod = await import(pathToFileURL(join(HERE, 'index.js')).href)
if (typeof mod.apply !== 'function') throw new Error('index.js 没有导出 apply()')
const inject = mod.inject
if (!Array.isArray(inject)) throw new Error('index.js 没有导出 inject 数组')

await mod.apply(ctx)

console.log(`inject = ${JSON.stringify(inject)}`)
console.log(`注册了 ${registered.size} 个工具:`)
for (const [name, def] of registered) {
  const props = Object.keys(def.parameters?.properties || {})
  console.log(`  - ${name}(${props.join(', ')})  ${String(def.description).slice(0, 60)}...`)
}

const missing = ['screen_read', 'screen_find', 'screen_click', 'screen_click_text', 'screen_batch_click',
  'screen_windows'].filter((n) => !registered.has(n))
if (missing.length) throw new Error('缺少工具: ' + missing.join(', '))

// 只读调用: 列窗口
const windows = registered.get('screen_windows')
const wvalue = await windows.execute({ action: 'list' }, { signal: undefined })
const wtext = windows.output.render({}, wvalue).map((r) => r.text).join('\n')
console.log('\n── screen_windows 实测 ──')
console.log(wtext.split('\n').slice(0, 6).join('\n'))

// 只读调用: 找字
const find = registered.get('screen_find')
const fargs = { text: '开票', region: '0,0,1000,280' }
const fvalue = await find.execute(fargs, { signal: undefined })
console.log('\n── screen_find 实测 (找「开票」) ──')
console.log(find.output.render(fargs, fvalue).map((r) => r.text).join('\n'))