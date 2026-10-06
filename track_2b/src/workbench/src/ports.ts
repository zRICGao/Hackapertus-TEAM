// Explicit future boundaries: no network transport is configured in the foundation.
export interface TestRequest {
    experiment_id: string
    case_id: string
    target_id: string
}
export interface ExperimentPort {
    test(request: TestRequest): Promise<{ attempt_id: string }>
}
export interface KnowledgePort {
    search(query: string, targetRevision: string): Promise<ReadonlyArray<{
        id: string
        review_status: string
        evidence_refs: string[]
    }>>
}
export class UnconfiguredExperimentPort implements ExperimentPort {
    async test(_request: TestRequest): Promise<{ attempt_id: string }> {
        throw new Error("Experiment API transport is not configured")
    }
}
