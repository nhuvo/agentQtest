"""
Performance Monitor Utility

WHAT: Tracks and reports agent performance metrics
WHY: Provides insights into agent efficiency, costs, and quality
HOW: Monitors execution time, token usage, success rates, and costs
"""

import time
import json
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict


class PerformanceMonitor:
    """
    WHAT: Performance tracking system for AI agents
    WHY: Monitor agent behavior and optimize performance
    HOW: Collect metrics during agent execution and generate reports
    """

    def __init__(self):
        """Initialize the performance monitor."""
        # WHAT: Storage for performance metrics
        # WHY: Track all interactions for analysis
        # HOW: Dictionaries to store different metric types
        self.interactions = []
        self.tool_usage = defaultdict(int)
        self.error_counts = defaultdict(int)
        self.start_time = time.time()

    def record_interaction(
        self,
        interaction_id: str,
        prompt: str,
        response: str,
        duration: float,
        tokens_used: int,
        tools_used: List[str],
        success: bool,
        error: Optional[str] = None,
        cost: Optional[float] = None
    ):
        """
        Record a single agent interaction.

        WHAT: Stores detailed metrics for one interaction
        WHY: Enables analysis of individual and aggregate performance
        HOW: Appends interaction data to internal storage

        Args:
            interaction_id: Unique identifier for this interaction
            prompt: User's input prompt
            response: Agent's response
            duration: Time taken to complete (seconds)
            tokens_used: Total tokens consumed
            tools_used: List of tools invoked
            success: Whether interaction completed successfully
            error: Error message if failed
            cost: Estimated cost in USD
        """
        # WHAT: Create interaction record
        # WHY: Store all relevant metrics
        # HOW: Dictionary with standardized fields
        interaction = {
            "interaction_id": interaction_id,
            "timestamp": datetime.now().isoformat(),
            "prompt": prompt[:100] + "..." if len(prompt) > 100 else prompt,
            "response_length": len(response),
            "duration": duration,
            "tokens_used": tokens_used,
            "tools_used": tools_used,
            "success": success,
            "error": error,
            "cost": cost
        }

        # WHAT: Store interaction
        # WHY: Build historical record
        # HOW: Append to list
        self.interactions.append(interaction)

        # WHAT: Update tool usage counts
        # WHY: Track which tools are used most
        # HOW: Increment counter for each tool
        for tool in tools_used:
            self.tool_usage[tool] += 1

        # WHAT: Track errors by type
        # WHY: Identify common failure modes
        # HOW: Count occurrences of each error
        if error:
            error_type = error.split(':')[0] if ':' in error else error
            self.error_counts[error_type] += 1

    def get_summary_stats(self) -> Dict:
        """
        Generate summary statistics for all interactions.

        WHAT: Aggregates performance metrics
        WHY: Provides high-level overview of agent performance
        HOW: Calculates averages, totals, and rates

        Returns:
            Dict: Summary statistics
        """
        if not self.interactions:
            return {
                "message": "No interactions recorded yet",
                "total_interactions": 0
            }

        # WHAT: Calculate basic counts
        # WHY: Understand volume and success rate
        # HOW: Sum up successes and failures
        total = len(self.interactions)
        successes = sum(1 for i in self.interactions if i["success"])
        failures = total - successes

        # WHAT: Calculate averages
        # WHY: Understand typical performance
        # HOW: Sum and divide by count
        avg_duration = sum(i["duration"] for i in self.interactions) / total
        avg_tokens = sum(i["tokens_used"] for i in self.interactions) / total
        total_tokens = sum(i["tokens_used"] for i in self.interactions)

        # WHAT: Calculate costs
        # WHY: Track spending on API calls
        # HOW: Sum all interaction costs
        interactions_with_cost = [i for i in self.interactions if i["cost"] is not None]
        total_cost = sum(i["cost"] for i in interactions_with_cost) if interactions_with_cost else 0
        avg_cost = total_cost / len(interactions_with_cost) if interactions_with_cost else 0

        # WHAT: Calculate uptime
        # WHY: Track how long monitor has been running
        # HOW: Current time minus start time
        uptime = time.time() - self.start_time

        # WHAT: Find slowest and fastest interactions
        # WHY: Identify performance outliers
        # HOW: Sort by duration
        sorted_by_duration = sorted(self.interactions, key=lambda x: x["duration"])
        fastest = sorted_by_duration[0] if sorted_by_duration else None
        slowest = sorted_by_duration[-1] if sorted_by_duration else None

        # WHAT: Compile summary dictionary
        # WHY: Return all key metrics
        # HOW: Dictionary with categorized statistics
        return {
            "overview": {
                "total_interactions": total,
                "successful": successes,
                "failed": failures,
                "success_rate": f"{(successes / total * 100):.1f}%",
                "uptime_seconds": f"{uptime:.1f}",
                "uptime_formatted": str(timedelta(seconds=int(uptime)))
            },
            "performance": {
                "avg_duration_seconds": f"{avg_duration:.3f}",
                "fastest_interaction": f"{fastest['duration']:.3f}s" if fastest else "N/A",
                "slowest_interaction": f"{slowest['duration']:.3f}s" if slowest else "N/A"
            },
            "tokens": {
                "total_tokens_used": total_tokens,
                "avg_tokens_per_interaction": f"{avg_tokens:.0f}"
            },
            "costs": {
                "total_cost": f"${total_cost:.4f}",
                "avg_cost_per_interaction": f"${avg_cost:.4f}",
                "interactions_with_cost_data": len(interactions_with_cost)
            },
            "tools": dict(self.tool_usage),
            "errors": dict(self.error_counts) if self.error_counts else {"No errors": 0}
        }

    def get_detailed_report(self) -> str:
        """
        Generate a detailed performance report.

        WHAT: Creates formatted text report of all metrics
        WHY: Human-readable performance analysis
        HOW: Formats summary stats with nice presentation

        Returns:
            str: Formatted performance report
        """
        stats = self.get_summary_stats()

        if "message" in stats:
            return stats["message"]

        # WHAT: Build formatted report
        # WHY: Make metrics easy to read and understand
        # HOW: Multi-line string with sections
        report = []
        report.append("=" * 70)
        report.append("AGENT PERFORMANCE REPORT")
        report.append("=" * 70)
        report.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"Monitor Uptime: {stats['overview']['uptime_formatted']}")

        # Overview section
        report.append("\n" + "-" * 70)
        report.append("OVERVIEW")
        report.append("-" * 70)
        report.append(f"Total Interactions: {stats['overview']['total_interactions']}")
        report.append(f"Successful: {stats['overview']['successful']}")
        report.append(f"Failed: {stats['overview']['failed']}")
        report.append(f"Success Rate: {stats['overview']['success_rate']}")

        # Performance section
        report.append("\n" + "-" * 70)
        report.append("PERFORMANCE METRICS")
        report.append("-" * 70)
        report.append(f"Average Duration: {stats['performance']['avg_duration_seconds']}s")
        report.append(f"Fastest Interaction: {stats['performance']['fastest_interaction']}")
        report.append(f"Slowest Interaction: {stats['performance']['slowest_interaction']}")

        # Token usage section
        report.append("\n" + "-" * 70)
        report.append("TOKEN USAGE")
        report.append("-" * 70)
        report.append(f"Total Tokens: {stats['tokens']['total_tokens_used']:,}")
        report.append(f"Average per Interaction: {stats['tokens']['avg_tokens_per_interaction']}")

        # Cost section
        report.append("\n" + "-" * 70)
        report.append("COSTS")
        report.append("-" * 70)
        report.append(f"Total Cost: {stats['costs']['total_cost']}")
        report.append(f"Average per Interaction: {stats['costs']['avg_cost_per_interaction']}")
        report.append(f"Tracked Interactions: {stats['costs']['interactions_with_cost_data']}")

        # Tool usage section
        report.append("\n" + "-" * 70)
        report.append("TOOL USAGE")
        report.append("-" * 70)
        if stats['tools']:
            for tool, count in sorted(stats['tools'].items(), key=lambda x: x[1], reverse=True):
                report.append(f"  {tool}: {count} times")
        else:
            report.append("  No tools used")

        # Errors section
        report.append("\n" + "-" * 70)
        report.append("ERRORS")
        report.append("-" * 70)
        if list(stats['errors'].keys()) == ["No errors"]:
            report.append("  No errors encountered")
        else:
            for error, count in sorted(stats['errors'].items(), key=lambda x: x[1], reverse=True):
                report.append(f"  {error}: {count} occurrences")

        report.append("\n" + "=" * 70)

        return "\n".join(report)

    def get_recent_interactions(self, count: int = 10) -> List[Dict]:
        """
        Get the most recent interactions.

        WHAT: Returns last N interactions
        WHY: Quick view of recent agent behavior
        HOW: Slice last N items from interactions list

        Args:
            count: Number of recent interactions to return

        Returns:
            List[Dict]: Recent interaction records
        """
        return self.interactions[-count:]

    def export_to_json(self, filepath: str):
        """
        Export all metrics to JSON file.

        WHAT: Saves performance data to file
        WHY: Persist metrics for later analysis
        HOW: Write JSON with all interactions and stats

        Args:
            filepath: Path to output JSON file
        """
        # WHAT: Prepare export data
        # WHY: Include both raw data and summary
        # HOW: Dictionary with interactions and stats
        export_data = {
            "export_timestamp": datetime.now().isoformat(),
            "summary": self.get_summary_stats(),
            "interactions": self.interactions
        }

        # WHAT: Write to file
        # WHY: Save for later reference
        # HOW: JSON dump with formatting
        with open(filepath, 'w') as f:
            json.dump(export_data, f, indent=2)

        print(f"Performance data exported to {filepath}")

    def reset(self):
        """
        Reset all performance metrics.

        WHAT: Clears all stored data
        WHY: Start fresh monitoring session
        HOW: Reinitialize all tracking variables
        """
        self.interactions = []
        self.tool_usage = defaultdict(int)
        self.error_counts = defaultdict(int)
        self.start_time = time.time()

    def get_tool_efficiency_report(self) -> Dict:
        """
        Analyze tool usage efficiency.

        WHAT: Calculate metrics per tool
        WHY: Understand which tools are most/least efficient
        HOW: Group interactions by tool and calculate averages

        Returns:
            Dict: Tool efficiency metrics
        """
        # WHAT: Group interactions by tool
        # WHY: Analyze each tool separately
        # HOW: Dictionary with tool as key
        tool_metrics = defaultdict(lambda: {
            "count": 0,
            "total_duration": 0,
            "total_tokens": 0,
            "successes": 0,
            "failures": 0
        })

        # WHAT: Aggregate metrics per tool
        # WHY: Calculate efficiency statistics
        # HOW: Loop through interactions and sum values
        for interaction in self.interactions:
            for tool in interaction["tools_used"]:
                metrics = tool_metrics[tool]
                metrics["count"] += 1
                metrics["total_duration"] += interaction["duration"]
                metrics["total_tokens"] += interaction["tokens_used"]
                if interaction["success"]:
                    metrics["successes"] += 1
                else:
                    metrics["failures"] += 1

        # WHAT: Calculate efficiency scores
        # WHY: Provide comparable metrics across tools
        # HOW: Compute averages and rates
        efficiency_report = {}
        for tool, metrics in tool_metrics.items():
            if metrics["count"] > 0:
                efficiency_report[tool] = {
                    "usage_count": metrics["count"],
                    "avg_duration": f"{metrics['total_duration'] / metrics['count']:.3f}s",
                    "avg_tokens": int(metrics['total_tokens'] / metrics['count']),
                    "success_rate": f"{(metrics['successes'] / metrics['count'] * 100):.1f}%"
                }

        return efficiency_report


# WHAT: Example usage demonstration
# WHY: Show how to use PerformanceMonitor
# HOW: Create monitor, record interactions, generate reports
if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("PERFORMANCE MONITOR DEMO")
    print("=" * 70 + "\n")

    # WHAT: Create monitor instance
    # WHY: Initialize performance tracking
    # HOW: Instantiate PerformanceMonitor
    monitor = PerformanceMonitor()

    # WHAT: Simulate some interactions
    # WHY: Demonstrate monitoring capabilities
    # HOW: Record sample interactions with various characteristics
    print("Simulating agent interactions...\n")

    # Interaction 1: Successful with file_search
    monitor.record_interaction(
        interaction_id="int_001",
        prompt="What is our PTO policy?",
        response="According to our policy, employees accrue 15-30 days of PTO based on tenure...",
        duration=2.345,
        tokens_used=523,
        tools_used=["file_search"],
        success=True,
        cost=0.0012
    )

    # Interaction 2: Successful with browser
    monitor.record_interaction(
        interaction_id="int_002",
        prompt="Check weather in Seattle",
        response="Current weather in Seattle is 52°F, partly cloudy with light rain expected...",
        duration=45.123,
        tokens_used=892,
        tools_used=["browser"],
        success=True,
        cost=0.0089
    )

    # Interaction 3: Successful with code_interpreter
    monitor.record_interaction(
        interaction_id="int_003",
        prompt="Analyze this CSV file",
        response="Analysis complete. The data shows an average delivery time of 2.3 days...",
        duration=8.756,
        tokens_used=1245,
        tools_used=["code_interpreter"],
        success=True,
        cost=0.0156
    )

    # Interaction 4: Failed interaction
    monitor.record_interaction(
        interaction_id="int_004",
        prompt="Generate report",
        response="",
        duration=1.234,
        tokens_used=234,
        tools_used=[],
        success=False,
        error="RateLimitError: Rate limit exceeded",
        cost=0.0003
    )

    # Interaction 5: Multi-tool success
    monitor.record_interaction(
        interaction_id="int_005",
        prompt="Research competitors and analyze our position",
        response="Based on web research and data analysis, here's our competitive position...",
        duration=67.891,
        tokens_used=2134,
        tools_used=["browser", "code_interpreter"],
        success=True,
        cost=0.0234
    )

    print("Recorded 5 sample interactions\n")

    # WHAT: Generate and display summary
    # WHY: Show high-level metrics
    # HOW: Call get_summary_stats and print
    print("-" * 70)
    print("SUMMARY STATISTICS")
    print("-" * 70)
    stats = monitor.get_summary_stats()
    print(json.dumps(stats, indent=2))

    # WHAT: Display detailed report
    # WHY: Show formatted performance analysis
    # HOW: Call get_detailed_report
    print("\n")
    print(monitor.get_detailed_report())

    # WHAT: Show tool efficiency
    # WHY: Demonstrate tool-specific metrics
    # HOW: Call get_tool_efficiency_report
    print("\n" + "-" * 70)
    print("TOOL EFFICIENCY REPORT")
    print("-" * 70)
    efficiency = monitor.get_tool_efficiency_report()
    for tool, metrics in efficiency.items():
        print(f"\n{tool}:")
        for key, value in metrics.items():
            print(f"  {key}: {value}")

    # WHAT: Show recent interactions
    # WHY: Demonstrate recent history view
    # HOW: Call get_recent_interactions
    print("\n" + "-" * 70)
    print("RECENT INTERACTIONS (Last 3)")
    print("-" * 70)
    recent = monitor.get_recent_interactions(count=3)
    for interaction in recent:
        print(f"\n{interaction['interaction_id']}: {interaction['prompt']}")
        print(f"  Duration: {interaction['duration']:.2f}s | Tokens: {interaction['tokens_used']}")
        print(f"  Tools: {', '.join(interaction['tools_used']) if interaction['tools_used'] else 'None'}")
        print(f"  Status: {'Success' if interaction['success'] else 'Failed'}")

    # WHAT: Export to file
    # WHY: Demonstrate data persistence
    # HOW: Call export_to_json
    print("\n" + "-" * 70)
    print("EXPORTING DATA")
    print("-" * 70)
    monitor.export_to_json("performance_report.json")

    print("\n" + "=" * 70)
    print("DEMO COMPLETE")
    print("=" * 70)
    print("\nPerformance monitoring helps you:")
    print("- Track agent efficiency and costs")
    print("- Identify bottlenecks and errors")
    print("- Optimize tool selection")
    print("- Monitor success rates")
    print("- Export data for analysis")
    print("=" * 70 + "\n")
