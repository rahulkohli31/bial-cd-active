import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-8 px-6 py-16">
      <div className="space-y-3">
        <h1 className="text-4xl font-semibold tracking-tight">Your BIAL app</h1>
        <p className="text-muted-foreground text-lg">
          This is the golden Next.js template. Describe what you want in the builder and the app
          takes shape here — starting from the example CRUD screen below.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Records</CardTitle>
          <CardDescription>
            An example list / create / edit / delete screen wired through the platform
            data-service. Use it as the pattern for your own data.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild>
            <Link href="/records">Open the Records screen</Link>
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}
