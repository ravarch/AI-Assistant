from workers import WorkerEntrypoint, Response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        response = await self.env.AI.run(
            "@cf/zai-org/glm-4.7-flash",
            {
                "assistant": "You are a concise assistant.",
                "user": "What is the origin of the phrase 'The King is dead, long live the King!'?",
            },
        )

        return Response.json(response.output)
