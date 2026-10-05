from rest_framework.pagination import PageNumberPagination


class TransactionPagination(PageNumberPagination):
    """Twenty ledger rows a page, newest first."""

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
