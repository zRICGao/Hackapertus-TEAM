import { resolve } from "node:path"
import {
    appendChallengeMemory, listChallengeMemory, addChallengeIdea, listChallengeIdeas,
} from "../vendor/breachweave/packages/core/src/challenge/memory"
import { formatMemoryTable, formatIdeaTable } from "../vendor/breachweave/packages/core/src/solver/extension/challenge-observer/board-format"
import type { MemoryKind } from "../vendor/breachweave/packages/core/src/challenge/memory"
export type { ObserverReviewPayload, ObserverReviewResult } from "../vendor/breachweave/packages/core/src/solver/extension/challenge-observer/types"

export class RuntimeState {
    private readonly root: string
    constructor(root: string, readonly experimentId: string) {
        if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(experimentId)) {
            throw new Error("Invalid experiment id")
        }
        this.root = resolve(root)
    }
    async remember(content: string, kind: MemoryKind = "note", refs: string[] = []) {
        return appendChallengeMemory(this.root, {
            challengeId: this.experimentId, content, kind, refs, source: "apertus-workbench",
        })
    }
    async propose(content: string) {
        return addChallengeIdea(this.root, this.experimentId, { content, status: "pending" })
    }
    async snapshot() {
        const memory = await listChallengeMemory(this.root, this.experimentId)
        const ideas = await listChallengeIdeas(this.root, this.experimentId)
        return { schema_version: "1.0", experiment_id: this.experimentId, memory, ideas }
    }
    async board() {
        const { memory, ideas } = await this.snapshot()
        return `${formatMemoryTable(memory)}\n\n${formatIdeaTable(ideas)}\n`
    }
}
