import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../../frontend/src/state/collectionState.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } });
const { CollectionState } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);
const status = (overrides = {}) => ({
    state: "running", total: 36000, completed: 35010, cached: 35000,
    processed: 10, processingElapsedSeconds: 20, errors: 0,
    currentFile: "image.jpg", message: "", species: [], ...overrides,
});

test("35,000 cached images do not inflate the processing rate", () => {
    const state = new CollectionState();
    state.startScan("scan");
    // The initial worker snapshots can be skipped by polling.
    state.applyStatus(status());
    assert.equal(state.estimatedFinishMs(100000) - 100000, 1980000);
    state.applyStatus(status({ total: 1000, completed: 10, cached: 0 }));
    assert.equal(state.estimatedFinishMs(100000) - 100000, 1980000);
});

test("no estimate before real processing, with a complete cache, or after termination", () => {
    const state = new CollectionState();
    for (const item of [
        status({ completed: 35000, processed: 0 }),
        status({ total: 35000, completed: 35000, processed: 0 }),
        status({ processed: 1000, completed: 36000 }),
        status({ state: "done" }), status({ state: "stopped" }), status({ state: "error" }),
        status({ processingElapsedSeconds: 0 }),
        status({ processingElapsedSeconds: NaN }),
        status({ cached: undefined, processed: undefined, processingElapsedSeconds: undefined }),
    ]) {
        state.applyStatus(item);
        assert.equal(state.estimatedFinishMs(100000), null);
    }
});

test("restart and retained UI progress cannot contaminate the new scan estimate", () => {
    const state = new CollectionState();
    state.applyStatus(status({ state: "stopped", completed: 35900, species: [{ speciesId: "bird" }] }));
    state.startScan("restart");
    assert.equal(state.estimatedFinishMs(), null);
    state.applyStatus(status());
    assert.equal(state.status.completed, 35900); // Existing UI continuity behavior.
    assert.equal(state.estimatedFinishMs(100000) - 100000, 1980000);
    state.clear();
    assert.equal(state.estimatedFinishMs(), null);
});
