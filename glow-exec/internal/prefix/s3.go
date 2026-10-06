package prefix

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awshttp "github.com/aws/aws-sdk-go-v2/aws/transport/http"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// Network timeouts. Objects can be large, so there is no limit on a whole
// transfer; instead connections, response headers and stalled reads time
// out. The SDK retries failed requests a bounded number of times.
const (
	dialTimeout           = 10 * time.Second
	responseHeaderTimeout = 60 * time.Second
	readTimeout           = 2 * time.Minute
)

// S3 is a Store over S3 or an S3-compatible service. Credentials, region
// and endpoint come from the standard AWS configuration (environment, web
// identity, instance role). AWS_ENDPOINT_URL_S3 selects another endpoint
// and GLOW_S3_PATH_STYLE=true enables path-style addressing, as MinIO
// usually needs.
type S3 struct {
	client *s3.Client
}

// NewS3 loads the AWS configuration and returns an S3 store.
func NewS3(ctx context.Context) (*S3, error) {
	httpClient := awshttp.NewBuildableClient().
		WithDialerOptions(func(d *net.Dialer) { d.Timeout = dialTimeout }).
		WithTransportOptions(func(t *http.Transport) { t.ResponseHeaderTimeout = responseHeaderTimeout }).
		WithReadTimeout(readTimeout)
	cfg, err := config.LoadDefaultConfig(ctx, config.WithHTTPClient(httpClient))
	if err != nil {
		return nil, err
	}
	pathStyle := os.Getenv("GLOW_S3_PATH_STYLE") == "true"
	client := s3.NewFromConfig(cfg, func(o *s3.Options) { o.UsePathStyle = pathStyle })
	return &S3{client: client}, nil
}

func splitS3(uri string) (string, string, error) {
	rest, ok := strings.CutPrefix(uri, "s3://")
	if !ok {
		return "", "", fmt.Errorf("%q is not an s3:// URI", uri)
	}
	bucket, key, _ := strings.Cut(rest, "/")
	if bucket == "" {
		return "", "", fmt.Errorf("%q has no bucket", uri)
	}
	return bucket, key, nil
}

// Download copies the object at uri to dest.
func (s *S3) Download(ctx context.Context, uri, dest string) error {
	bucket, key, err := splitS3(uri)
	if err != nil {
		return err
	}
	out, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)})
	if err != nil {
		return fmt.Errorf("downloading %s: %w", uri, err)
	}
	defer out.Body.Close()
	if err := writeAtomic(dest, out.Body); err != nil {
		return fmt.Errorf("downloading %s: %w", uri, err)
	}
	return nil
}

// Upload copies src to uri with a single PutObject request, which limits
// one object to 5 GB.
func (s *S3) Upload(ctx context.Context, src, uri, mediaType string) error {
	bucket, key, err := splitS3(uri)
	if err != nil {
		return err
	}
	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()
	input := &s3.PutObjectInput{Bucket: aws.String(bucket), Key: aws.String(key), Body: f}
	if mediaType != "" {
		input.ContentType = aws.String(mediaType)
	}
	if _, err := s.client.PutObject(ctx, input); err != nil {
		return fmt.Errorf("uploading %s: %w", uri, err)
	}
	return nil
}

// List returns the keys under the prefix uri, relative to it. Directory
// marker objects (keys ending in "/") are skipped.
func (s *S3) List(ctx context.Context, uri string) ([]string, error) {
	bucket, key, err := splitS3(uri)
	if err != nil {
		return nil, err
	}
	keyPrefix := key
	if keyPrefix != "" && !strings.HasSuffix(keyPrefix, "/") {
		keyPrefix += "/"
	}
	var paths []string
	pages := s3.NewListObjectsV2Paginator(s.client, &s3.ListObjectsV2Input{
		Bucket: aws.String(bucket), Prefix: aws.String(keyPrefix),
	})
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return nil, fmt.Errorf("listing %s: %w", uri, err)
		}
		for _, object := range page.Contents {
			rel := strings.TrimPrefix(aws.ToString(object.Key), keyPrefix)
			if rel != "" && !strings.HasSuffix(rel, "/") {
				paths = append(paths, rel)
			}
		}
	}
	return paths, nil
}
