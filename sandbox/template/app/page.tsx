import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function Home() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col justify-center gap-6 px-6 py-16">
      <div className="space-y-3">
        <h1 className="text-4xl font-semibold tracking-tight">Your BIAL app</h1>
        <p className="text-muted-foreground text-lg">
          A blank starting point. Describe what you want to build and the app takes shape here —
          replace this page with your own UI.
        </p>
      </div>

      <div>
        <Button asChild variant="outline">
          <Link href="/records">See an example data screen</Link>
        </Button>
      </div>
    </main>
  );
}
