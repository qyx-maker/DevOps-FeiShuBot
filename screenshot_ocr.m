#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

int main(void) {
    @autoreleasepool {
        NSData *image = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = NO;
        request.minimumTextHeight = 0.003;
        request.usesCPUOnly = YES;
        NSSet *supported = [NSSet setWithArray:[request supportedRecognitionLanguagesAndReturnError:NULL] ?: @[]];
        NSMutableArray *languages = [NSMutableArray array];
        for (NSString *language in @[@"en-US", @"zh-Hans"]) {
            if ([supported containsObject:language]) {
                [languages addObject:language];
            }
        }
        if (languages.count) {
            request.recognitionLanguages = languages;
        }
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithData:image options:@{}];
        NSError *error = nil;
        if (![handler performRequests:@[request] error:&error]) {
            fprintf(stderr, "OCR failed: %s (%ld)\n", error.domain.UTF8String, (long)error.code);
            return 1;
        }
        for (VNRecognizedTextObservation *line in request.results) {
            NSString *text = [line topCandidates:1].firstObject.string;
            if (text.length) {
                puts(text.UTF8String);
            }
        }
    }
    return 0;
}
