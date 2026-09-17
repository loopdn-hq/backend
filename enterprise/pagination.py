from rest_framework.pagination import PageNumberPagination


class CreditLedgerPagination(PageNumberPagination):
    """Twenty a page, matching the project default."""

    page_size_query_param = 'page_size'
    max_page_size = 100
