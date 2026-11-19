import { Link } from "react-router";

import { PageHeader } from "../components/PageHeader";

export function NotFoundPage() {
  return (
    <section>
      <PageHeader title="Not found" />
      <div className="card">
        <p>This page or request does not exist, or you are not allowed to see it.</p>
        <p>
          <Link className="button" to="/requests">
            Back to requests
          </Link>
        </p>
      </div>
    </section>
  );
}
