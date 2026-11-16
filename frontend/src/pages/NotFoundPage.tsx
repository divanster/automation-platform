import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <section>
      <h1>Not found</h1>
      <p>This page or request does not exist, or you are not allowed to see it.</p>
      <p>
        <Link to="/requests">Back to requests</Link>
      </p>
    </section>
  );
}
