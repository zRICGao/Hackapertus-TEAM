import { RuntimeState } from "./runtime-state"

const root = process.argv[2]
if (!root) throw new Error("Usage: bun run src/demo.ts <state-directory>")
const state = new RuntimeState(root, "foundation-check")
await state.propose("Check the offline fixture with a matched control")
await state.remember("This is a mock demonstration, not an Apertus finding")
console.log(JSON.stringify(await state.snapshot(), null, 2))
