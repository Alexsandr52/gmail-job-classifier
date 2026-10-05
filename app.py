"""Run the complete Gmail fetch, classify, and cleanup workflow."""

from utils.email_reader import main as fetch_new_emails
from utils.processor import main as classify_and_clean


def main():
    fetch_new_emails()
    classify_and_clean()


if __name__ == "__main__":
    main()
