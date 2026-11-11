/**
 * dsh-screen-kit —— 让 agent 真的能看屏幕、动鼠标。
 *
 * 七个工具, 全部走本地 OCR + 各平台原生输入接口, 不经过任何大模型:
 *   screen_read        截图并 OCR, 返回带坐标的文字行
 *   screen_find        在一段区域里找文字, 返回"像不像按钮"的排序候选 (不点击)
 *   screen_click       按像素坐标点击
 *   screen_key         按功能键 (esc/enter/tab/方向键), 故意不支持打字
 *   screen_click_text  找到那段文字并点它 (拒绝模糊匹配和状态文字)
 *   screen_batch_click 连续点同一套按钮: OCR 确认 + 自带停止条件
 *   screen_windows     列窗口 / 把某个窗口切到前台
 *
 * 支持 Windows / macOS / Linux (X11); Wayland 只能截图, 见 README。
 *
 * 慢的根源与对策 (Windows 实测):
 *   全屏 OCR 13 秒  -> 一律先裁剪小区域再 OCR (0.7 秒)
 *   python 冷启动 + 模型加载 1.5 秒 -> 整批点击在同一个 python 进程里跑完
 */
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'

const HERE = dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'))
const BRIDGE = join(HERE, 'bridge.py')

const text = (s) => [{ type: 'text', text: s }]

/** 找 Python: 环境变量 > PATH 里的 python / python3 / py */
async function resolvePython(ctx) {
  const explicit = process.env.DSH_SCREEN_KIT_PYTHON || process.env.DSH_PYTHON
  if (explicit && existsSync(explicit)) return explicit
  for (const name of ['python3', 'python', 'py']) {
    try {
      return await ctx.subprocess.resolveExecutable(name)
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error(
    '找不到 python。装好 Python 3.9+, 并 pip install rapidocr_onnxruntime Pillow numpy ' +
      '(macOS 上动鼠标还要 pyobjc-framework-Quartz, Linux 截屏推荐 mss), ' +
      '或用 DSH_SCREEN_KIT_PYTHON 指定解释器路径。',
  )
}

/** 跑一次 bridge.py, 拿回解析好的 JSON。 */
async function runBridge(ctx, command, args = {}, signal) {
  const python = await resolvePython(ctx)
  const handle = ctx.subprocess.spawn({
    argv: [python, '-u', BRIDGE, command, JSON.stringify(args)],
    cwd: HERE,
    env: {
      PYTHONIOENCODING: 'utf-8',
    },
    stdio: {
      stdin: 'ignore',
      stdout: { maxBytes: 8 << 20 },
      stderr: { maxBytes: 2 << 20 },
    },
    graceMs: 10_000,
    signal,
  })
  const outcome = await handle.done
  const stdout = handle.collected.stdout ? handle.collected.stdout.readFrom(0).text : ''
  const stderr = handle.collected.stderr ? handle.collected.stderr.readFrom(0).text : ''
  const trimmed = stdout.trim()
  if (!trimmed) {
    throw new Error(
      `bridge 没有输出 (exit ${outcome.exitCode})\n${stderr.trim() || '(无 stderr)'}`,
    )
  }
  let parsed
  try {
    parsed = JSON.parse(trimmed.split('\n').filter(Boolean).pop())
  } catch {
    throw new Error(`bridge 输出不是 JSON:\n${trimmed.slice(0, 2000)}`)
  }
  if (parsed && parsed.ok === false) {
    const err = new Error(parsed.error || 'bridge 失败')
    err.detail = parsed
    throw err
  }
  return parsed
}

/** 把失败也变成可读输出, 而不是一个裸异常 —— 模型看得懂才改得对。 */
function failure(err) {
  const detail = err && err.detail ? err.detail : null
  const lines = [`失败: ${err && err.message ? err.message : String(err)}`]
  if (detail && detail.hint) lines.push(`提示: ${detail.hint}`)
  if (detail && detail.near_misses && detail.near_misses.length) {
    lines.push('', '屏幕上相近的文字:')
    for (const m of detail.near_misses.slice(0, 10)) lines.push(`- ${m}`)
  }
  return text(lines.join('\n'))
}

export const inject = ['tools', 'subprocess']

export async function apply(ctx) {
  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_read',
      description:
        'Read the screen as text WITH coordinates (local OCR, no model call). ' +
        'Returns every text line with pixel (x,y) and normalized (nx,ny) coordinates in 0..1000. ' +
        'ALWAYS pass `region` if you know roughly where to look: full-screen OCR costs ~13s here, ' +
        'a small region costs ~0.7s. Use this to find out what is actually on screen before clicking.',
      parameters: {
        type: 'object',
        properties: {
          region: {
            type: 'string',
            description:
              'Normalized box "x1,y1,x2,y2" in 0..1000 to limit OCR to. Strongly recommended.',
          },
        },
        additionalProperties: false,
      },
      output: {
        schema: {
          type: 'object',
          properties: {
            count: { type: 'number' },
            text: { type: 'string' },
            blocks: { type: 'array' },
          },
          additionalProperties: true,
        },
        render: (_args, value) => text(value.text || '(屏幕上没有文字)'),
      },
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        try {
          return await runBridge(ctx, 'read', { region: args.region || '' }, exec.signal)
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_find',
      description:
        'Locate a piece of text on screen and return ranked candidates (does NOT click). ' +
        'Each candidate carries tier: 0 = the text is a whole OCR segment (a real button in a row of ' +
        'buttons whose labels OCR glued together), 1 = the text starts its block (a label), 2 = the text ' +
        'is buried inside other words (usually status text). ' +
        'This matters: searching "开票" on a billing screen also matches the status label ' +
        '"开票状态：待开票", and clicking that does nothing. Prefer tier 0. ' +
        'Pass `region` to disambiguate and to stay fast.',
      parameters: {
        type: 'object',
        properties: {
          text: { type: 'string', description: 'The text to look for.' },
          region: { type: 'string', description: 'Normalized box "x1,y1,x2,y2" in 0..1000.' },
          exact: {
            type: 'boolean',
            description:
              'Default true = the text must really appear. Set false to also allow loose ' +
              'character-overlap matches, which are marked fuzzy and are NOT safe to click.',
          },
          limit: { type: 'number', description: 'Max candidates to return (default 20).' },
        },
        required: ['text'],
        additionalProperties: false,
      },
      output: {
        schema: {
          type: 'object',
          properties: {
            found: { type: 'boolean' },
            best: { type: 'object' },
            hits: { type: 'array' },
          },
          additionalProperties: true,
        },
        render: (_args, value) => {
          if (!value.found) return text('屏幕上找不到这段文字。用 screen_read 看看实际有什么。')
          const lines = [`找到 ${value.count} 处:`]
          for (const h of value.hits) {
            const where = h.tier === 0 ? '独立控件' : h.tier === 1 ? '块首' : '夹在正文里'
            const warn = h.fuzzy ? ' [模糊匹配, 别点]' : ''
            lines.push(
              `- (${h.x},${h.y}) 归一化(${h.nx},${h.ny}) 档${h.tier} ${where}${warn}  整行: ${h.block}`,
            )
          }
          const best = value.best
          lines.push('', `最优候选: (${best.x},${best.y}) 归一化(${best.nx},${best.ny})`)
          if (value.count > 1) {
            lines.push('注意: 有多处同名, 确认你要的是哪一个 (用 region 收窄最稳)。')
          }
          return text(lines.join('\n'))
        },
      },
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        try {
          return await runBridge(
            ctx,
            'find',
            {
              text: args.text,
              region: args.region || '',
              exact: args.exact !== false,
              limit: args.limit || 20,
            },
            exec.signal,
          )
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_click',
      description:
        'Click at pixel coordinates (what screen_read / screen_find report). ' +
        'The mouse really moves. Prefer screen_click_text when you know the label text.',
      parameters: {
        type: 'object',
        properties: {
          x: { type: 'number', description: 'Pixel x.' },
          y: { type: 'number', description: 'Pixel y.' },
          button: { type: 'string', enum: ['left', 'right', 'middle'] },
          clicks: { type: 'number', description: 'How many clicks (default 1).' },
        },
        required: ['x', 'y'],
        additionalProperties: false,
      },
      output: {
        schema: {
          type: 'object',
          properties: { clicked: { type: 'array' }, normalized: { type: 'array' } },
          additionalProperties: true,
        },
        render: (_args, value) => text(`已点击像素 (${value.clicked[0]},${value.clicked[1]})`),
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        try {
          return await runBridge(
            ctx,
            'click',
            { x: args.x, y: args.y, button: args.button || 'left', clicks: args.clicks || 1 },
            exec.signal,
          )
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_key',
      description:
        'Press one or more keys in the focused window: esc, enter, tab, space, backspace, delete, ' +
        'up/down/left/right, home/end, pageup/pagedown. Deliberately NOT a general keyboard ' +
        'simulator - there is no text typing here, because that would hand over the ability to type ' +
        'into any window. Use it to dismiss a dialog (esc) or confirm one (enter).',
      parameters: {
        type: 'object',
        properties: {
          keys: {
            type: 'array',
            items: { type: 'string' },
            description: "Key names, e.g. ['esc'] or ['enter']. Earlier keys are pressed first.",
          },
        },
        required: ['keys'],
        additionalProperties: false,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => text(`已按键: ${(value.pressed || []).join(' + ')}`),
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        try {
          return await runBridge(ctx, 'key', { keys: args.keys || [] }, exec.signal)
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_click_text',
      description:
        'Find a piece of text and click it (one step, no separate coordinate guessing). ' +
        'It clicks the CENTER OF THE MATCHED WORDS, not the center of the OCR block - this is what ' +
        'prevents the classic miss where OCR glues a row of buttons into "医保退费追溯码开票打印中", ' +
        'clicking the block center lands on "追溯码", and you meant "开票". ' +
        'It refuses fuzzy character-overlap matches and (by default) refuses text buried inside other ' +
        'words, so it will not click a status label. Reports the other same-name candidates it did NOT click.',
      parameters: {
        type: 'object',
        properties: {
          text: { type: 'string', description: 'The label to click.' },
          region: { type: 'string', description: 'Normalized box "x1,y1,x2,y2" in 0..1000.' },
          max_tier: {
            type: 'number',
            description:
              'How loose a candidate may be: 1 (default) = only real controls/labels, ' +
              '2 = also allow text buried in prose.',
          },
        },
        required: ['text'],
        additionalProperties: false,
      },
      output: {
        schema: {
          type: 'object',
          properties: {
            clicked: { type: 'array' },
            text: { type: 'string' },
            block: { type: 'string' },
            position: { type: 'string' },
            others_same_name: { type: 'array' },
          },
          additionalProperties: true,
        },
        render: (_args, value) => {
          const lines = [
            `已点击 ${value.text} @像素(${value.clicked[0]},${value.clicked[1]}) ` +
              `归一化(${value.normalized[0]},${value.normalized[1]})`,
            `它所在的整行: ${value.block}`,
            `命中位置: ${value.position}`,
          ]
          if (value.others_same_name && value.others_same_name.length) {
            lines.push('', '另外还有同名的地方, 点的不是它们:')
            for (const o of value.others_same_name) {
              lines.push(`- (${o.nx},${o.ny}) ${o.block}`)
            }
          }
          return text(lines.join('\n'))
        },
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        try {
          return await runBridge(
            ctx,
            'click_text',
            { text: args.text, region: args.region || '', max_tier: args.max_tier ?? 1 },
            exec.signal,
          )
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_batch_click',
      description:
        'Click the same pair of buttons over and over (the "issue N invoices" pattern), entirely in ' +
        'local OCR - no model call per step, so it runs ~8s per item instead of ~25s. ' +
        'Each round: click `top_text` in top_region, wait until `popup_text` really appears in ' +
        'popup_region (OCR-verified, not a pixel-diff guess), click it, wait until it disappears. ' +
        'It stops by itself on: `empty_text` appearing (e.g. a "暂无可开发票" dialog), the button never ' +
        'appearing after a retry, the dialog not closing, or reaching `times`. ' +
        'Regions are normalized 0..1000 and are REQUIRED to be tight - that is how two buttons with the ' +
        'same label (one in the toolbar, one inside the dialog) are told apart.',
      parameters: {
        type: 'object',
        properties: {
          top_text: { type: 'string', description: 'Label of the first button (default "开票").' },
          popup_text: {
            type: 'string',
            description: 'Label of the confirm button in the dialog. Defaults to top_text.',
          },
          top_region: {
            type: 'string',
            description: 'Normalized "x1,y1,x2,y2" where the first button lives.',
          },
          popup_region: {
            type: 'string',
            description: 'Normalized "x1,y1,x2,y2" where the dialog button lives.',
          },
          times: { type: 'number', description: 'Max rounds (default 20).' },
          interval: { type: 'number', description: 'Seconds between clicks (default 2).' },
          empty_text: {
            type: ['string', 'array'],
            items: { type: 'string' },
            description:
              'When this text shows up, stop and report it - e.g. "暂无可开发票" means the queue is empty. ' +
              'Pass an array to cover several phrasings at once (one screenshot is checked against all of ' +
              'them, so it costs nothing extra): the same system often says "暂无可开发票" in a dialog but ' +
              '"暂无患者" in the list when nothing is left.',
          },
          absent_means_empty: {
            type: 'boolean',
            description:
              'Treat a vanished `top_text` button as "queue empty" instead of as an error. ' +
              'Some systems show no dialog when there is nothing left to click - they just ' +
              'remove the button. Costs nothing to enable when the two cases coincide.',
          },
          wait_limit: { type: 'number', description: 'Max seconds to wait for the dialog (default 10).' },
          dry_run: {
            type: 'boolean',
            description:
              'Locate both buttons and report their coordinates WITHOUT clicking anything. ' +
              'Run this first when you are not sure the regions are right.',
          },
        },
        required: ['top_region', 'popup_region'],
        additionalProperties: false,
      },
      output: {
        schema: {
          type: 'object',
          properties: { done: { type: 'number' }, stopped: { type: 'string' }, log: { type: 'array' } },
          additionalProperties: true,
        },
        render: (_args, value) => {
          if (value.dry_run) {
            const t = value.top_button
            const p = value.popup_button
            const lines = [
              '试运行 (没有点击任何东西)',
              `起始按钮 ${t.text} @像素(${t.x},${t.y}) 档${t.tier}  整行: ${t.block}`,
              value.popup_open
                ? `弹窗里已有 ${p.text} @像素(${p.x},${p.y})`
                : '弹窗当前没开 (真跑时它会点起始按钮、等弹窗出现、再点弹窗里那个)',
            ]
            return text(lines.join('\n'))
          }
          const lines = [`完成 ${value.done} 次`, `停止原因: ${value.stopped}`]
          if (value.log && value.log.length) {
            lines.push('', '过程:')
            for (const l of value.log.slice(-25)) lines.push(`- ${l}`)
          }
          return text(lines.join('\n'))
        },
      },
      isConcurrencySafe: () => false,
      async execute(args, exec) {
        try {
          return await runBridge(
            ctx,
            'batch',
            {
              top_text: args.top_text || '开票',
              popup_text: args.popup_text || args.top_text || '开票',
              top_region: args.top_region,
              popup_region: args.popup_region,
              times: args.times || 20,
              interval: args.interval ?? 2.0,
              empty_text: args.empty_text || '',
              wait_limit: args.wait_limit ?? 10.0,
              dry_run: args.dry_run === true,
            },
            exec.signal,
          )
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )

  ctx.effect(() =>
    ctx.tools.register({
      name: 'screen_windows',
      description:
        'List visible windows with their bounds, or bring one to the front. ' +
        'Use action="focus" with a title fragment before clicking, so keystrokes and clicks land in the ' +
        'intended window.',
      parameters: {
        type: 'object',
        properties: {
          action: { type: 'string', enum: ['list', 'focus'], description: 'Default "list".' },
          title: { type: 'string', description: 'Title fragment to focus (case-insensitive).' },
        },
        additionalProperties: false,
      },
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => {
          if (value.focused) return text(`已切到前台: ${value.focused}`)
          const lines = [`共 ${value.count} 个窗口:`]
          for (const w of value.windows || []) {
            lines.push(`- ${w.title}  [${w.app}]  ${w.rect.join(',')}`)
          }
          return text(lines.join('\n'))
        },
      },
      isConcurrencySafe: () => true,
      async execute(args, exec) {
        try {
          return await runBridge(
            ctx,
            'windows',
            { action: args.action || 'list', title: args.title || '' },
            exec.signal,
          )
        } catch (err) {
          return failure(err)
        }
      },
    }),
  )
}