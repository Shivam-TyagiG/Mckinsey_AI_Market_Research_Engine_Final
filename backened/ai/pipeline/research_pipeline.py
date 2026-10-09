import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

from ai.browser.tavily_search import TavilySearchEngine
from ai.llm.gemini import GeminiLLM
from ai.planner.planner_agent import PlannerAgent
from ai.research.research_agent import ResearchAgent
from ai.extraction.extraction_agent import ExtractionAgent
from ai.validation.validation_agent import ValidationAgent
from ai.report.report_agent import ReportAgent
from ai.report.citation_builder import CitationBuilder
from ai.report.report_linker import ReportLinker

from ai.schemas.research_result import ResearchResult
from ai.schemas.research_task import ResearchTask
from ai.schemas.source import Source
from ai.schemas.evidence import Evidence
from ai.schemas.validation import ValidationResult
from ai.schemas.citation import Citation
from ai.schemas.report_item import ReportItem
from ai.schemas.linked_report import LinkedReport, LinkedReportItem

logger = logging.getLogger(__name__)

# ==========================================================
# Pipeline Limits
# ==========================================================

MAX_RESEARCH_TASKS = 6
MAX_EXTRACTION_SOURCES = 6       # Extract only from best 6 sources
MAX_VALIDATION_EVIDENCE = 30     # Validate top 30 evidence
MAX_REPORT_EVIDENCE = 20         # Report uses top 20 evidence


class ResearchPipeline:
    def __init__(
        self,
        planner=None,
        researcher=None,
        extractor=None,
        validator=None,
        reporter=None,
        citation_builder=None,
        report_linker=None,
    ):
        llm = GeminiLLM()
        browser = TavilySearchEngine()
        self.planner = planner or PlannerAgent(llm=llm)
        self.researcher = researcher or ResearchAgent(search_engine=browser)
        self.extractor = extractor or ExtractionAgent(llm=llm, browser=browser)
        self.validator = validator or ValidationAgent(llm=llm)
        self.reporter = reporter or ReportAgent(llm=llm)
        self.citation_builder = citation_builder or CitationBuilder()
        self.report_linker = report_linker or ReportLinker()

    @staticmethod
    def _canonical_url(url: str) -> str:
        parsed = urlsplit(url.strip())
        return urlunsplit((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        ))

    @staticmethod
    def _failure_summary(error: Exception) -> str:
        detail = " ".join(str(error).split())
        detail = re.sub(
            r"(?i)(api[_-]?key|access[_-]?token|authorization)(\s*[:=]\s*)\S+",
            r"\1\2[redacted]",
            detail,
        )
        detail = detail[:180] or "no provider detail returned"
        return f"{type(error).__name__}: {detail}"

    @staticmethod
    def _fallback_task(query: str) -> ResearchTask:
        return ResearchTask(
            task_id="task_fallback_001",
            query=query,
            purpose="Broad research using the original brief because planning was unavailable.",
        )

    @staticmethod
    def _unique_tasks(tasks: list[ResearchTask]) -> list[ResearchTask]:
        unique_tasks = []
        seen_queries = set()
        seen_ids = set()

        for index, task in enumerate(tasks, start=1):
            normalized_query = " ".join(task.query.casefold().split())
            if not normalized_query or normalized_query in seen_queries:
                continue

            if task.task_id in seen_ids:
                task = task.model_copy(update={"task_id": f"task_deduplicated_{index:03d}"})

            seen_queries.add(normalized_query)
            seen_ids.add(task.task_id)
            unique_tasks.append(task)
            if len(unique_tasks) >= MAX_RESEARCH_TASKS:
                break

        return unique_tasks

    @staticmethod
    def _fallback_citations(sources: list[Source]) -> list[Citation]:
        return [
            Citation(
                citation_id=f"citation_{index:03d}",
                source_id=source.source_id,
                title=source.title,
                url=source.url,
                publisher=source.publisher,
                published_date=source.published_date,
            )
            for index, source in enumerate(sources, start=1)
        ]

    @staticmethod
    def _fallback_report(
        tasks: list[ResearchTask],
        evidences: list[Evidence],
        citations: list[Citation],
        warnings: list[str],
    ):
        title = next((task.query.splitlines()[0].strip() for task in tasks if task.query.strip()), "Research brief")
        title = f"{title[:100]}: Evidence Brief (Fallback)"
        return ReportAgent.build_evidence_fallback(
            title=title,
            evidences=evidences,
            citations=citations,
            warnings=warnings,
        )

    @staticmethod
    def _source_fallback_report(query: str, citations: list[Citation], warnings: list[str]):
        title = query.splitlines()[0].strip() if query.strip() else "Research brief"
        return ReportAgent.build_source_fallback(
            title=title,
            citations=citations,
            warnings=warnings,
        )

    @staticmethod
    def _fallback_linked_report(report):
        def unlinked(items):
            return [LinkedReportItem(text=item.text, sources=[]) for item in items]

        return LinkedReport(
            title=report.title,
            executive_summary=report.executive_summary,
            key_findings=unlinked(report.key_findings),
            market_signals=unlinked(report.market_signals),
            competitor_observations=unlinked(report.competitor_observations),
            implications=unlinked(report.implications),
            recommendations=unlinked(report.recommendations),
            evidence_appendix=report.evidence_appendix,
            citations=report.citations,
        )

    # ==========================================================
    # Main Research Pipeline
    # ==========================================================

    def run(self, query: str) -> ResearchResult:

        pipeline_start = time.time()
        warnings: list[str] = []

        try:

            # ======================================================
            # STEP 1 — Planner
            # ======================================================

            logger.info("========== PLANNER STAGE ==========")

            stage_start = time.time()

            try:
                tasks: list[ResearchTask] = self.planner.create_plan(query)
            except Exception as error:
                logger.warning("Planner failed; using one broad research task: %s", error)
                warnings.append("Planning was unavailable; research used the original brief.")
                tasks = []

            if not tasks:
                if not warnings:
                    warnings.append("Planner returned no tasks; research used the original brief.")
                tasks = [self._fallback_task(query)]

            tasks = self._unique_tasks(tasks)
            if not tasks:
                warnings.append("The plan contained no unique research tasks; research used the original brief.")
                tasks = [self._fallback_task(query)]

            logger.info(
                "Planner generated %d research tasks in %.2fs.",
                len(tasks),
                time.time() - stage_start,
            )
            # above we generate a list of research tasks using the planner agent. 
            # If the planner fails or returns no tasks, we create a fallback task using the original query. 
            # We also ensure that the tasks are unique and log the number of tasks generated and the time taken for this step.
            #so that means if the planner fails or returns no tasks, we create a fallback task using the original query. 
            # and we continue with the research pipeline using this fallback task. and generate a report based on the fallback task and any sources and evidence we can find.
            # task contains task_id, query and purpose. so we can use the query to search for sources and evidence. and we can use the purpose to understand what the task is about.



            # ======================================================
            # STEP 2 — Research
            # ======================================================

            logger.info("========== RESEARCH STAGE ==========")

            stage_start = time.time()

            sources: list[Source] = []
# In this we are calling tavily search engine to search for sources based on the research tasks generated by the planner.
            seen_urls = set()
            for task in tasks:
                try:
                    task_sources = self.researcher.research(task)
                except Exception as error:
                    logger.warning("Research failed for task %s: %s", task.task_id, error)
                    warnings.append(f"Search failed for one task: {task.purpose}")
                    continue

                for source in task_sources:
                    canonical_url = self._canonical_url(source.url)
                    if not canonical_url or canonical_url in seen_urls:
                        continue
                    seen_urls.add(canonical_url)
                    sources.append(source)

            if not sources:
                raise ValueError("Research agent returned no sources.")

            logger.info(
                "Research found %d sources in %.2fs.",
                len(sources),
                time.time() - stage_start,
            )

            # ======================================================
            # STEP 3 — Extraction
            # ======================================================

            logger.info("========== EXTRACTION STAGE ==========")

            stage_start = time.time()

            evidences: list[Evidence] = []
            seen_evidence_ids = set()

            sources_to_extract = sources[:MAX_EXTRACTION_SOURCES]

            logger.info(
                "Extracting evidence from %d of %d sources.",
                len(sources_to_extract),
                len(sources),
            )

            for index, source in enumerate(sources_to_extract, start=1):

                try:
                    source_evidence = self.extractor.extract(source)
                except Exception as error:
                    logger.warning(
                        "Extraction failed for source %s (%d/%d): %s",
                        source.source_id,
                        index,
                        len(sources_to_extract),
                        error,
                    )
                    warnings.append(
                        f"Evidence extraction failed for source: {source.title} "
                        f"({self._failure_summary(error)})"
                    )
                    continue

                if not source_evidence:
                    warnings.append(f"No usable evidence was extracted from source: {source.title}")
                    continue

                for evidence in source_evidence:
                    if evidence.source_id != source.source_id:
                        logger.warning(
                            "Ignoring evidence with mismatched source id from %s.",
                            source.source_id,
                        )
                        continue
                    if evidence.evidence_id in seen_evidence_ids:
                        continue
                    seen_evidence_ids.add(evidence.evidence_id)
                    evidences.append(evidence)

            if not evidences:
                warnings.append(
                    "Sources were found, but no usable evidence claims could be extracted."
                )

            logger.info(
                "Extraction produced %d evidence items in %.2fs.",
                len(evidences),
                time.time() - stage_start,
            )

            # ======================================================
            # STEP 4 — Validation
            # ======================================================

            logger.info("========== VALIDATION STAGE ==========")

            stage_start = time.time()

            validation_evidence = sorted(
                evidences,
                key=lambda x: getattr(x, "relevance_score", 0),
                reverse=True,
            )[:MAX_VALIDATION_EVIDENCE]

            logger.info(
                "Validating top %d evidence items out of %d.",
                len(validation_evidence),
                len(evidences),
            )

            if validation_evidence:
                try:
                    validations: list[ValidationResult] = self.validator.validate(
                        evidences=validation_evidence,
                        sources=sources,
                    )
                except Exception as error:
                    logger.warning("Evidence validation failed; continuing with unvalidated evidence: %s", error)
                    warnings.append("Evidence validation was unavailable; report claims are not independently validated.")
                    validations = []
            else:
                validations = []

            if evidences and not validations and not any("validation" in warning.lower() for warning in warnings):
                warnings.append("No evidence validation results were available; report claims are not independently validated.")
            elif validations and len(validations) < len(evidences):
                warnings.append("Some evidence could not be validated; unvalidated claims are identified in the report warnings.")

            logger.info(
                "Validation produced %d results in %.2fs.",
                len(validations),
                time.time() - stage_start,
            )

            # ======================================================
            # STEP 5 — Citation Builder
            # ======================================================

            logger.info("========== CITATION STAGE ==========")

            stage_start = time.time()

            try:
                citations = self.citation_builder.build(sources)
            except Exception as error:
                logger.warning("Citation builder failed; using source metadata: %s", error)
                warnings.append("Citation formatting used source metadata fallback.")
                citations = self._fallback_citations(sources)

            if not citations:
                citations = self._fallback_citations(sources)
                warnings.append("Citation formatting used source metadata fallback.")

            logger.info(
                "Generated %d citations in %.2fs.",
                len(citations),
                time.time() - stage_start,
            )

            # ======================================================
            # STEP 6 — Report Generation
            # ======================================================

            logger.info("========== REPORT STAGE ==========")

            stage_start = time.time()

            if len(validation_evidence) > MAX_REPORT_EVIDENCE:
                top_evidence = sorted(
                    validation_evidence,
                    key=lambda x: getattr(x, "relevance_score", 0),
                    reverse=True,
                )[:MAX_REPORT_EVIDENCE]
            else:
                top_evidence = validation_evidence

            validation_map = {item.evidence_id: item for item in validations}
            top_evidence = [
                evidence
                for evidence in top_evidence
                if evidence.evidence_id not in validation_map
                or validation_map[evidence.evidence_id].is_valid
            ]
            if evidences and not top_evidence:
                raise ValueError("No evidence passed validation; a report cannot be generated safely.")

            if not top_evidence:
                report = self._source_fallback_report(query, citations, warnings)
            else:
                try:
                    report = self.reporter.generate_report(
                        tasks=tasks,
                        evidences=top_evidence,
                        validations=validations,
                        citations=citations,
                    )
                    if not report:
                        raise ValueError("Report agent returned no report.")
                except Exception as error:
                    logger.warning("Report synthesis failed; creating an evidence-only report: %s", error)
                    warnings.append("AI report synthesis was unavailable; this is an evidence-only fallback report.")
                    report = self._fallback_report(tasks, top_evidence, citations, warnings)

            report.warnings = list(dict.fromkeys(warnings))

            logger.info(
                "Report generated in %.2fs.",
                time.time() - stage_start,
            )

            # ======================================================
            # STEP 7 — Report Linking
            # ======================================================

            logger.info("========== REPORT LINKER STAGE ==========")

            stage_start = time.time()

            try:
                linked_report = self.report_linker.link_report(
                    report=report,
                    evidences=evidences,
                    citations=citations,
                )
            except Exception as error:
                logger.exception("Report linking failed; returning an unlinked report: %s", error)
                warnings.append("Automatic source linking was unavailable; citations remain in the report appendix.")
                report.warnings = list(dict.fromkeys(warnings))
                linked_report = self._fallback_linked_report(report)

            if not linked_report:
                raise ValueError(
                    "Report linker returned no linked report."
                )

            logger.info(
                "Linked %d key findings in %.2fs.",
                len(linked_report.key_findings),
                time.time() - stage_start,
            )

            logger.info(
                "Pipeline completed successfully in %.2fs.",
                time.time() - pipeline_start,
            )

            return ResearchResult(
                report=report,
                linked_report=linked_report,
                tasks=tasks,
                sources=sources,
                evidences=evidences,
                validations=validations,
                warnings=list(dict.fromkeys(warnings)),
            )

        # ======================================================
        # Error Handling
        # ======================================================

        except Exception:
            logger.exception(
                "Research pipeline failed after %.2fs.",
                time.time() - pipeline_start,
            )
            raise
