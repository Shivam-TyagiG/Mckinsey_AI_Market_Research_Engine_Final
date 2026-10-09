from backend.db.supabase_client import supabase


class ResearchJobRepository:
# here we are defining a class called ResearchJobRepository that will be used to interact with the research_jobs table in the Supabase database. 
# This class contains methods for creating, listing, retrieving, and updating research jobs.

#this method is used to create a new research job in the database. 
#It takes a brief description of the research job and an optional created_by parameter that specifies the user who created the job.

    def create_job(self, brief: str, created_by: str | None = None) -> dict: 
        payload = {
            "brief": brief,
            "status": "pending",
        }

        if created_by:
            payload["created_by"] = created_by

        response = (
            supabase
            .table("research_jobs")
            .insert(payload)
            .execute()
        )

        if not response.data:
            raise RuntimeError("Failed to create research job")

        return response.data[0]

    def list_jobs(self, created_by: str | None = None, limit: int = 50) -> list[dict]:
        query = (
            supabase
            .table("research_jobs")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
# query.eq mens we are added a filter to the query to only return jobs that were created by the specified user.
# If created_by is None, then we will return all jobs.
        if created_by:
            query = query.eq("created_by", created_by)

        response = query.execute()

        return response.data or []

    def get_job(self, job_id: str) -> dict | None:
        response = (
            supabase
            .table("research_jobs")
            .select("*")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )

        if not response.data:
            return None

        return response.data[0]

    def update_status(self, job_id: str, status: str) -> dict:
        response = (
            supabase
            .table("research_jobs")
            .update({
                "status": status,
            })
            .eq("id", job_id)
            .execute()
        )

        if not response.data:
            raise RuntimeError("Failed to update research job")

        return response.data[0]