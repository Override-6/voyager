// REPL driver for Node: runs code chunks in one persistent global scope, like the node shell.
//
// Protocol (stdin): one JSON object per line, {"id": str, "code": str}. After each chunk it prints
// MARK + " <id> ok|error" on its own line; everything before that is the chunk's output.
// A chunk runs as a script, so top-level const/let/function persist and the last expression's value is shown.
// A chunk with top-level `await` runs as an async function instead; its top-level `const x =` / `let x =` lines
// become globals so they still persist. Errors thrown later by event handlers are printed, never fatal.
'use strict'
const vm = require('vm')
const util = require('util')
const readline = require('readline')
const { createRequire } = require('module')
const path = require('path')

const MARK = '\u0000REPL-DONE'
globalThis.require = createRequire(path.join(process.cwd(), 'noop.js')) // resolves the cwd's node_modules

process.on('uncaughtException', (e) => console.error('[uncaught]', e && e.stack ? e.stack : e))
process.on('unhandledRejection', (e) => console.error('[unhandled rejection]', e && e.stack ? e.stack : e))

function show (value) {
  // shallow on purpose: a live object (a client, a socket) would flood the context
  if (value !== undefined) console.log(util.inspect(value, { depth: 1, maxArrayLength: 30, maxStringLength: 2000, breakLength: 120 }))
}

async function runChunk (code, file) {
  if (file) { // a file's definitions: run as a script (its function declarations become globals)
    globalThis.module = { exports: {} }
    globalThis.exports = globalThis.module.exports
    new vm.Script(code, { filename: file }).runInThisContext()
    return
  }
  let script
  try {
    script = new vm.Script(code, { filename: 'repl' })
  } catch (e) {
    if (!(e instanceof SyntaxError) || !/await/.test(code)) throw e
    const body = code.replace(/^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/gm, 'globalThis.$1 =')
    show(await vm.runInThisContext(`(async () => {\n${body}\n})()`, { filename: 'repl' }))
    return
  }
  let value = script.runInThisContext()
  if (value && typeof value.then === 'function') value = await value
  show(value)
}

const queue = []
let busy = false
async function drain () {
  if (busy) return
  busy = true
  while (queue.length) {
    const msg = queue.shift()
    let ok = true
    try {
      await runChunk(msg.code, msg.file)
    } catch (e) {
      ok = false
      console.error(e && e.stack ? e.stack : e)
    }
    process.stdout.write(`${MARK} ${msg.id} ${ok ? 'ok' : 'error'}\n`)
  }
  busy = false
}

readline.createInterface({ input: process.stdin }).on('line', (line) => {
  if (!line.trim()) return
  queue.push(JSON.parse(line))
  drain()
})
