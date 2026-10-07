import styles from "./stone-mark.module.css";

export function StoneMark({
  size = "compact",
  className = "",
}: {
  size?: "compact" | "large";
  className?: string;
}) {
  return (
    <span className={`${styles.mark} ${styles[size]} ${className}`} aria-hidden="true">
      <span className={`${styles.stone} ${styles.black}`} />
      <span className={`${styles.stone} ${styles.white}`} />
    </span>
  );
}
