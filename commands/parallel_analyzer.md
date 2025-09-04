# Parallel Implementation Analyzer

**CRITICAL** This command analyzes a plan document to determine parallelization opportunities for implementation.

## Arguments
`$ARGUMENTS` should be the name of a `plan*.md` file in the project root. For example:
- `plan_wrapper_removal.md`
- `plan_enum_generation.md`
- `plan_mutation_system.md`

## Overview

This command performs deep analysis of a plan document to identify which implementation tasks can be executed in parallel vs sequentially, accounting for our agentic testing workflow and Rust compilation dependencies.

## Analysis Workflow

<AnalysisSteps>
    **EXECUTE THESE STEPS IN ORDER:**
    
    **STEP 1:** Execute <PlanIngestion/>
    **STEP 2:** Execute <TaskExtraction/>
    **STEP 3:** Execute <DependencyAnalysis/>
    **STEP 4:** Execute <ParallelizationEvaluation/>
    **STEP 5:** Execute <PackageGeneration/>
    **STEP 6:** Execute <FinalReport/>
</AnalysisSteps>

## STEP 1: PLAN INGESTION

<PlanIngestion>
    1. Read the specified plan document from project root
    2. Parse the document structure:
       - Identify phases/steps/sections
       - Extract file paths mentioned
       - Note implementation tasks vs validation tasks
       - Identify dependencies between sections
    3. Create a structured representation of all tasks
</PlanIngestion>

## STEP 2: TASK EXTRACTION

<TaskExtraction>
    **Extract and categorize all implementation tasks:**
    
    1. **Implementation Tasks**: Code changes, file modifications, deletions
    2. **Validation Tasks**: Builds, tests, clippy checks  
    3. **External Dependencies**: Restarts, user interventions, agentic tests
    4. **Documentation Tasks**: Comments, README updates, plan updates
    
    For each task, record:
    - Target files/modules
    - Type of change (create, modify, delete, refactor)
    - Estimated complexity (trivial, simple, moderate, complex)
    - Dependencies on other tasks
</TaskExtraction>

## STEP 3: DEPENDENCY ANALYSIS

<DependencyAnalysis>
    **Analyze dependencies between tasks using Rust-specific rules:**
    
    <RustDependencyRules>
        **HARD DEPENDENCIES (Must be sequential):**
        - Deleting files that other tasks import
        - Changing function signatures that other tasks call
        - Modifying enum variants that other tasks pattern match
        - Removing struct fields that other tasks access
        - Renaming types/modules that other tasks reference
        - API changes that break compilation
        
        **SOFT DEPENDENCIES (Can be managed):**
        - Adding new files (no conflicts)
        - Adding new functions/methods (no conflicts)
        - Adding new fields to structs (if non-breaking)
        - Creating new modules (if no circular dependencies)
        - Independent bug fixes in different areas
        
        **BUILD DEPENDENCIES:**
        - Tasks modifying same files → Sequential required
        - Tasks in same compilation unit → May conflict
        - Tasks affecting shared dependencies → Coordination needed
        
        **EXTERNAL DEPENDENCIES:**
        - Restart required → All prior work must complete
        - User intervention → Blocking point
        - Agentic testing → Sequential validation phase
    </RustDependencyRules>
    
    **Output dependency graph showing:**
    - Which tasks block others
    - Which tasks can run independently  
    - Critical path through dependencies
    - Parallelizable clusters
</DependencyAnalysis>

## STEP 4: PARALLELIZATION EVALUATION

<ParallelizationEvaluation>
    **Evaluate each potential parallel package:**
    
    <EvaluationCriteria>
        **GOOD CANDIDATES (High parallelization value):**
        - Tasks creating entirely new files
        - Independent feature additions
        - Separate module implementations
        - Non-overlapping utility functions
        - Independent test file creation
        - Documentation for separate components
        
        **POOR CANDIDATES (Low parallelization value):**
        - Trivial complexity tasks
        - Single-file modifications
        - Tightly coupled refactoring
        - API changes affecting multiple files
        - Tasks requiring immediate coordination
        
        **DISQUALIFYING FACTORS:**
        - External dependencies (restarts, user input)
        - Agentic testing requirements
        - Shared file modifications
        - Breaking changes that cascade
        - Build system modifications
    </EvaluationCriteria>
    
    **For each parallelizable cluster:**
    1. Assess work complexity
    2. Evaluate setup overhead vs benefit
    3. Identify coordination points needed
    4. Assess risk of merge conflicts
    5. Determine if parallelization is worthwhile
</ParallelizationEvaluation>

## STEP 5: PACKAGE GENERATION

<PackageGeneration>
    **Generate work packages for viable parallel opportunities:**
    
    **Only create packages if:**
    - Parallelizable work > 20% of total implementation
    - Individual packages have meaningful complexity (beyond trivial)
    - Coordination overhead < benefit gained
    - No external blocking dependencies within packages
    
    **Package Structure:**
    ```json
    {
      "package_id": "pkg_1",
      "description": "Brief description of work package",
      "complexity": "moderate",
      "files": ["src/new_module.rs", "tests/new_module_test.rs"],
      "tasks": [
        "Create new module with core functionality",
        "Implement helper functions",
        "Add comprehensive tests"
      ],
      "dependencies": [],
      "coordination_points": ["Final integration test"],
      "risk_level": "low"
    }
    ```
    
    **Batch Structure:**
    ```json
    {
      "batch_id": 1,
      "packages": ["pkg_1", "pkg_2"],
      "can_execute_parallel": true,
      "completion_criteria": "All packages pass individual builds"
    }
    ```
</PackageGeneration>

## STEP 6: FINAL REPORT

<FinalReport>
    **Generate comprehensive analysis report:**
    
    # Parallel Implementation Analysis: [PLAN_NAME]
    
    ## Executive Summary
    - **Total Tasks**: [count]
    - **Parallelizable**: [percentage]% ([count] tasks)
    - **Sequential Required**: [percentage]% ([count] tasks)  
    - **Recommendation**: [PARALLELIZE | SEQUENTIAL | HYBRID]
    
    ## Parallelization Breakdown
    
    ### Phase Analysis
    [For each phase, show what can/cannot be parallelized and why]
    
    ### Work Package Summary
    [If packages were generated]
    - **Total Packages**: [count]
    - **Complexity Distribution**: [breakdown by complexity level]
    - **Setup Overhead**: [relative assessment]
    - **Net Benefit**: [positive/negative/minimal]
    
    ### Critical Path
    [Show the longest dependency chain through the work]
    
    ### Risk Assessment
    - **Merge Conflict Risk**: [low/medium/high]
    - **Coordination Complexity**: [low/medium/high]
    - **Build Integration Risk**: [low/medium/high]
    
    ## Recommendation Details
    
    **If PARALLELIZE recommended:**
    - List generated work packages
    - Coordination strategy
    - Success criteria for each batch
    
    **If SEQUENTIAL recommended:**
    - Primary blocking factors
    - Alternative optimization suggestions
    
    **If HYBRID recommended:**
    - Which phases can be parallelized
    - Sequential checkpoints required
    - Mixed execution strategy
    
    ## Implementation Notes
    - External dependencies that block automation
    - Manual intervention points
    - Testing strategy implications
    - Rollback considerations
</FinalReport>

## Analysis Rules

<AnalysisConstraints>
    **Environment-Specific Rules:**
    1. **Agentic Testing**: Any plan requiring agentic validation cannot be fully automated
    2. **Restart Dependencies**: MCP changes require restart - creates hard sequential boundary
    3. **Build System**: Rust compilation creates natural coordination points
    4. **File Conflicts**: Multiple agents modifying same file = coordination nightmare
    5. **Type System**: Changes to core types cascade through entire codebase
    
    **Minimum Viability Thresholds:**
    - Individual packages must have meaningful complexity (beyond trivial)
    - Parallelizable work must be > 20% of total implementation  
    - Setup overhead must be justified by benefit gained
    - No more than 2 external coordination points per batch
    
    **Automatic Disqualifiers:**
    - Plans with > 80% sequential dependencies
    - Plans requiring mid-implementation user intervention
    - Refactoring plans affecting > 10 files
    - Plans with external service dependencies
</AnalysisConstraints>

## Output Format

The analysis displays a detailed report to the user. If parallelization is viable, it can optionally generate a **work_packages.json** file in the project root upon user request.

**CRITICAL**: Only recommend parallelization if it provides meaningful benefit over sequential implementation. Many plans are naturally sequential and forcing parallelization creates more problems than it solves.